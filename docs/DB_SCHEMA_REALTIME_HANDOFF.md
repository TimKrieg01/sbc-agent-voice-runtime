# DB Schema Handoff: Realtime Sorcery SIP Runtime

This document is the backend integration contract for the stateless SBC design.

## 1. Goal

Database is the single runtime source of truth for:
- SIP trunk admission (host, source CIDR, optional auth user)
- route selection (`backend_url`)
- tenant/session profile passed to ARI
- Asterisk PJSIP objects loaded via Sorcery realtime (`ps_*` tables)

No per-trunk static files are required on VM.

## 2. Canonical Runtime Function

Asterisk dialplan calls this DB function via `func_odbc`:

```sql
resolve_inbound_route(
  p_ingress_host  text,
  p_called_number text,
  p_auth_user     text,
  p_source_ip     text
) returns text
```

Return payload format (pipe-delimited):

`decision|trunk_id|route_id|backend_url|tenant_id|stt_engine|languages_csv|reject_reason|reject_cause`

Rules:
- `decision` is `allow` or `reject`
- if `allow`, `trunk_id`, `route_id`, `backend_url`, `tenant_id` must be present
- if `reject`, `reject_reason` and `reject_cause` must be set

Current reject cause defaults:
- `21` (Call Rejected)
- `3` (No Route to Destination) for no route match
- `41` (Temporary Failure) for DB/no-result fallback in dialplan

Typical reject reasons:
- `missing_host`
- `unknown_host`
- `ambiguous_host`
- `missing_source_ip`
- `invalid_source_ip`
- `source_ip_not_allowed`
- `auth_user_not_allowed`
- `no_matching_route`

## 3. Required Business Tables

Defined in `deploy/sql/config_schema.sql`.

### `organizations`
- tenant boundary
- `slug` is stable tenant key

### `inbound_trunks`
- trunk metadata (`provider`, `stt_engine`, `languages_csv`, active state)
- references `organizations`

### `trunk_ingress_hosts`
- hostnames used for trunk identity
- unique active host globally (`lower(host)` unique where active)

### `trunk_auth_users`
- optional auth user allowlist per trunk
- unique active auth user globally

### `trunk_source_cidrs`
- source IP allowlist per trunk (CIDR type)

### `routing_rules`
- route candidates per trunk
- fields:
  - `priority` (lower wins)
  - `called_number_pattern` (Postgres regex)
  - `backend_url` (must be https)
  - `is_active`

## 4. Required Asterisk Realtime Tables (`ps_*`)

Sorcery reads these directly:
- `ps_endpoints`
- `ps_aors`
- `ps_auths`
- `ps_endpoint_id_ips`
- `ps_domain_aliases`

Minimum required object set per active trunk:
1. Business policy rows in `inbound_trunks`, `trunk_ingress_hosts`, and `routing_rules`
2. Optional policy rows in `trunk_source_cidrs` and `trunk_auth_users`
3. One generic inbound endpoint row in `ps_endpoints` with `id='anonymous'`
4. One generic inbound AOR row in `ps_aors` with `id='anonymous'`

Recommended endpoint defaults:
- `context = inbound-realtime`
- `disallow = all`
- `allow = ulaw`
- `direct_media = no`
- `rtp_symmetric = yes`
- `rewrite_contact = yes`
- `force_rport = yes`

## 5. Backend Write Contract (Important)

Use a single transaction when changing trunk runtime state:
1. update business tables
2. upsert corresponding `ps_*` rows
3. commit
4. trigger Asterisk reload (`pjsip reload`)

On delete/deactivate:
1. mark business rows inactive (or delete if policy permits)
2. remove/update corresponding `ps_*` rows
3. commit
4. trigger `pjsip reload`

Do not leave business and `ps_*` out of sync.

### Recommended Object Naming

Use deterministic identifiers so backend upserts are idempotent:
- endpoint id: `av-<trunk_id>`
- aor id: `av-<trunk_id>`
- auth id (if used): `av-auth-<trunk_id>`
- identify rows: one row per CIDR and optional host header matcher

This keeps projection logic simple and reversible.

## 6. Route Decision Semantics

`resolve_inbound_route` logic order:
1. ingress host must map to exactly one active trunk
2. if trunk has source CIDRs configured, source IP must match one of them
3. if trunk has auth users configured, auth user must match
4. routing rule regex must match called number, or the single active route becomes the default fallback
5. best route by `priority`

If any step fails -> `reject`.

## 7. Data Hygiene Rules

- No `|` allowed in fields used inside payload (already constrained where relevant)
- enforce lowercase host/auth at write time
- avoid ambiguous regex routes by using explicit priorities
- keep `called_number_pattern` anchored when possible (`^\\+49...$`)

## 8. Initial Provisioning Checklist

Before enabling traffic:
1. insert `organizations`
2. insert `inbound_trunks`
3. insert `trunk_ingress_hosts`
4. insert `trunk_source_cidrs`
5. insert `routing_rules`
6. upsert `ps_*` objects for endpoint/identify
7. `pjsip reload`
8. test with representative INVITE host/number/source IP

## 9. Backend Transaction Templates

### 9.1 Upsert/Activate Trunk (single transaction)

1. Upsert business rows:
- `organizations`
- `inbound_trunks`
- `trunk_ingress_hosts`
- `trunk_source_cidrs`
- `routing_rules`

2. Ensure generic Sorcery projection exists:
- `ps_endpoints(id='anonymous', context='inbound-realtime', aors='anonymous')`
- `ps_aors(id='anonymous')`
- `ps_endpoint_id_ips` rows are not required for normal host-based routing

3. Commit transaction.
4. Trigger `pjsip reload` on the VM.

### 9.2 Deactivate/Delete Trunk (single transaction)

1. Set business rows inactive (or delete based on policy).
2. Remove any per-trunk `ps_auths` rows if they exist.
3. Commit transaction.
4. Trigger `pjsip reload`.

## 10. Verification Queries (Backend + Ops)

Use these to verify projection consistency:

```sql
-- Active trunk policy rows
select t.id, o.slug, t.is_active
from inbound_trunks t
join organizations o on o.id = t.org_id
where t.is_active = true and o.is_active = true;

-- Realtime endpoint projection rows
select e.id, e.context, e.aors
from ps_endpoints e
where e.id = 'anonymous';

-- Generic inbound endpoint presence
select e.id, e.context, e.aors
from ps_endpoints e
where e.id = 'anonymous';
```
## 11. File References

- SQL schema: `deploy/sql/config_schema.sql`
- Dialplan precheck consumer: `deploy/asterisk/extensions.conf`
- ODBC function mapping: `deploy/asterisk/func_odbc.conf`
