# DB Schema Handoff: Realtime SIP Runtime Contract

This document is the backend-facing integration contract for the current SBC/runtime design.

It reflects the live architecture now in use:

- one generic inbound Asterisk endpoint (`anonymous`)
- host-based trunk identification in the dialplan
- route resolution in PostgreSQL via `resolve_inbound_route(...)`
- optional trunk-level TLS enforcement
- RTP or SRTP accepted on the shared inbound endpoint, depending on peer negotiation

Important identity decision:

- all **business/entity IDs** in the runtime schema are UUIDs
- human-readable names such as `slug`, hostnames, and provider labels are not primary keys
- Asterisk realtime object names such as `anonymous` remain string identifiers because Asterisk expects semantic object names there

## 1. Runtime Model

Database is the runtime source of truth for:

- which inbound trunks exist
- which ingress hosts belong to which trunk
- which source CIDRs are allowed for a trunk
- which optional auth users are allowed for a trunk
- which route/backend should receive a given called number
- whether TLS signaling is required for a trunk
- which tenant/STT/language profile is passed into ARI/Stasis

Current tenant model:

- `organizations` is the tenant table
- one tenant can own multiple inbound trunks
- `inbound_trunks.org_id -> organizations.id`

Important architectural point:

- the backend does **not** provision one Asterisk endpoint per trunk anymore
- Asterisk uses a single generic inbound endpoint named `anonymous`
- trunk identity is resolved dynamically from the inbound `To` host plus DB policy

## 2. Canonical Route Resolution Function

Asterisk dialplan calls this DB function through `func_odbc`:

```sql
resolve_inbound_route(
  p_ingress_host  text,
  p_called_number text,
  p_auth_user     text,
  p_source_ip     text,
  p_transport     text
) returns text
```

Current payload format is **caret-delimited**:

`decision^trunk_id^route_id^backend_url^tenant_id^stt_engine^languages_csv^reject_reason^reject_cause`

Rules:

- `decision` is `allow` or `reject`
- if `allow`, these must be set:
  - `trunk_id` (UUID string)
  - `route_id` (UUID string)
  - `backend_url`
  - `tenant_id` (UUID string)
- if `reject`, these should be set:
  - `reject_reason`
  - `reject_cause`

Important:

- `reject_cause` is now treated as a **SIP response code** for `PJSIPHangup(...)`
- this is no longer the older Q.850-style cause-code contract
- UUIDs are returned as text inside the function payload because the dialplan transport format is string-based

Current reject reasons and SIP codes:

- `missing_host` -> `400`
- `invalid_source_ip` -> `400`
- `unknown_host` -> `404`
- `ambiguous_host` -> `503`
- `tls_required` -> `403`
- `missing_source_ip` -> `403`
- `source_ip_not_allowed` -> `403`
- `auth_user_not_allowed` -> `403`
- `no_matching_route` -> `404`
- dialplan fallback for DB/no-result -> `503`

## 3. Business Tables

Defined in `deploy/sql/config_schema.sql`.

### `organizations`

Tenant boundary. This is the current tenant entity.

Key fields:

- `id` (`uuid`, primary key)
- `slug`
- `display_name`
- `is_active`

Backend guidance:

- `id` is the canonical tenant identifier used in relational joins and returned as `tenant_id` from the runtime function
- `slug` is the stable human-readable tenant key for URLs, admin UI, backend APIs, and business-level lookups
- one organization/tenant can own multiple trunks

### `inbound_trunks`

One logical inbound SIP trunk.

Key fields:

- `id` (`uuid`, primary key)
- `org_id` (`uuid`, foreign key to organizations.id)
- `provider`
- `stt_engine`
- `languages_csv`
- `require_tls`
- `is_active`

Semantics:

- `require_tls = false`
  - UDP or TLS signaling is accepted
- `require_tls = true`
  - only TLS signaling is accepted
  - UDP signaling is rejected by `resolve_inbound_route(...)`

### `trunk_ingress_hosts`

Maps inbound hostnames to trunks.

Key fields:

- `id` (`uuid`, primary key)
- `trunk_id` (`uuid`, foreign key to inbound_trunks.id)
- `host`
- `is_active`

Semantics:

- this is the primary trunk identity mechanism
- an inbound request host must map to exactly one active trunk
- active hosts are unique globally by `lower(host)`

Example:

- `testcustomer.sip.agentvoiceruntime.com` -> trunk UUID `550e8400-e29b-41d4-a716-446655440000`

### `trunk_source_cidrs`

Optional source-IP allowlist per trunk.

Key fields:

- `id` (`uuid`, primary key)
- `trunk_id` (`uuid`, foreign key to inbound_trunks.id)
- `cidr`
- `is_active`

Semantics:

- if a trunk has **no** active CIDR rows, source-IP restriction is skipped
- if a trunk has one or more active CIDR rows, the inbound source IP must match one of them

### `trunk_auth_users`

Optional auth-user allowlist per trunk.

Key fields:

- `id` (`uuid`, primary key)
- `trunk_id` (`uuid`, foreign key to inbound_trunks.id)
- `auth_user`
- `is_active`

Semantics:

- if a trunk has **no** active auth-user rows, auth-user restriction is skipped
- if a trunk has one or more active auth-user rows, the inbound auth user must match one of them

Note:

- many carrier-style inbound trunks will not use this at all
- host + source CIDR is usually the primary admission policy

### `routing_rules`

Routing candidates for a trunk.

Key fields:

- `id` (`uuid`, primary key)
- `trunk_id` (`uuid`, foreign key to inbound_trunks.id)
- `priority`
- `called_number_pattern`
- `backend_url`
- `is_active`

Semantics:

- regex match is evaluated against the called number
- lower `priority` wins
- if no regex route matches, but there is exactly one active route on the trunk, that single route becomes the fallback default

## 4. Asterisk Realtime Tables (`ps_*`)

Sorcery still uses realtime tables, but in the current model they are **generic bootstrap**, not per-trunk projection.

Relevant tables:

- `ps_endpoints`
- `ps_aors`
- `ps_auths`
- `ps_endpoint_id_ips`
- `ps_domain_aliases`

Current intended minimum runtime rows:

1. one generic endpoint row:
   - `ps_endpoints.id = 'anonymous'`
2. one generic AOR row:
   - `ps_aors.id = 'anonymous'`
3. no per-trunk identify rows are required for normal host-based routing

Important:

- backend systems should **not** create one `ps_endpoints` row per trunk in the current architecture
- backend systems should **not** rely on `ps_endpoint_id_ips` for trunk matching in the normal path
- the `ps_*` tables are Asterisk-internal runtime projection tables and intentionally still use string object names like `anonymous`
- UUID standardization applies to the business schema the product/backend/frontend owns, not to Asterisk symbolic object IDs

The generic inbound endpoint is intentionally shared across all trunks and currently uses:

- `context = inbound-realtime`
- `disallow = all`
- `allow = ulaw`
- `direct_media = no`
- `rtp_symmetric = yes`
- `rewrite_contact = yes`
- `force_rport = yes`

Media behavior:

- signaling transport policy is decided per trunk via `require_tls`
- media is shared/generic:
  - plain RTP is accepted
  - SRTP via SDES is accepted when offered by the peer
- SRTP is **not** currently enforced per trunk

## 5. Runtime Resolution Logic

`resolve_inbound_route(...)` currently evaluates policy in this order:

1. normalize ingress host, auth user, transport
2. ingress host must map to exactly one active trunk
3. if `require_tls = true`, transport must be `tls`
4. if source CIDR rows exist, source IP must match one of them
5. if auth-user rows exist, auth user must match one of them
6. choose best active `routing_rules` row matching called number by priority
7. if no regex match exists, but there is exactly one active route on the trunk, use that route
8. otherwise reject

Dialplan inputs:

- `p_ingress_host`
  - extracted from the inbound `To` header host
- `p_called_number`
  - dialed number / extension
- `p_auth_user`
  - currently sourced from `X-Twilio-Username` when present
- `p_source_ip`
  - parsed from first `Via` hop
- `p_transport`
  - `tls` when the inbound top `Via` starts with `SIP/2.0/TLS`
  - otherwise `udp`

## 6. Backend Write Contract

Use a single transaction when changing trunk runtime state.

### Provision / update trunk

Write business tables:

- `organizations`
- `inbound_trunks`
- `trunk_ingress_hosts`
- optional `trunk_source_cidrs`
- optional `trunk_auth_users`
- `routing_rules`

Also ensure the generic inbound projection exists:

- `ps_endpoints(id='anonymous')`
- `ps_aors(id='anonymous')`

Then:

1. commit
2. trigger Asterisk reload or restart on the VM

### Deactivate / delete trunk

1. mark business rows inactive, or delete them if your policy allows hard delete
2. do **not** remove the shared `anonymous` endpoint unless you are decommissioning the entire runtime
3. commit
4. trigger Asterisk reload or restart

Important:

- the generic `anonymous` projection belongs to the runtime as a whole, not to any one trunk
- frontend/backend applications should treat the UUIDs in the business tables as authoritative record IDs
- frontend/backend applications should not use Asterisk symbolic object names as business IDs

## 7. Recommended Data Hygiene

- lowercase hosts before insert/update
- lowercase auth users before insert/update
- keep `called_number_pattern` anchored when possible
- avoid ambiguous route regexes; use explicit priorities
- do not put caret `^` separators into fields that are returned by the route function payload

## 8. Provisioning Checklist

Before enabling traffic for a new trunk:

1. insert or upsert `organizations`
2. insert or upsert `inbound_trunks`
3. insert one or more `trunk_ingress_hosts`
4. insert optional `trunk_source_cidrs`
5. insert optional `trunk_auth_users`
6. insert one or more `routing_rules`
7. ensure generic `anonymous` endpoint/AOR rows exist
8. reload or restart Asterisk
9. test:
   - known host over expected transport
   - unknown host rejection
   - if `require_tls = true`, UDP rejection and TLS acceptance

## 9.1 UUID Conventions For Backend/Frontend

The product layer should assume:

- `organizations.id` is a UUID
- `inbound_trunks.id` is a UUID
- `trunk_ingress_hosts.id` is a UUID
- `trunk_source_cidrs.id` is a UUID
- `trunk_auth_users.id` is a UUID
- `routing_rules.id` is a UUID

Recommended usage:

- use UUIDs for relational joins, API resource IDs, and update/delete operations
- use `slug`, `display_name`, `host`, and other readable fields for display and lookup UX
- do not infer business meaning from UUID values

Important migration note:

- this is a breaking schema-contract change from older text-based IDs such as `org1-main`
- existing environments must migrate legacy IDs before backend/frontend assume UUID-only records

## 10. Verification Queries

### Show active trunks and their routing policy

```sql
SELECT
    t.id AS trunk_id,
    t.org_id AS tenant_id,
    o.slug AS org_slug,
    o.display_name AS org_name,
    t.provider,
    t.stt_engine,
    t.languages_csv,
    t.require_tls,
    t.is_active AS trunk_active,
    ARRAY_REMOVE(ARRAY_AGG(DISTINCT h.host) FILTER (WHERE h.is_active), NULL) AS ingress_hosts,
    ARRAY_REMOVE(ARRAY_AGG(DISTINCT c.cidr::text) FILTER (WHERE c.is_active), NULL) AS source_cidrs,
    ARRAY_REMOVE(ARRAY_AGG(DISTINCT a.auth_user) FILTER (WHERE a.is_active), NULL) AS auth_users,
    ARRAY_REMOVE(
        ARRAY_AGG(
            DISTINCT CONCAT(
                r.id,
                ' [priority=',
                r.priority,
                ', pattern=',
                r.called_number_pattern,
                ', backend=',
                r.backend_url,
                ', active=',
                r.is_active,
                ']'
            )
        ) FILTER (WHERE r.id IS NOT NULL),
        NULL
    ) AS routes
FROM inbound_trunks t
JOIN organizations o ON o.id = t.org_id
LEFT JOIN trunk_ingress_hosts h ON h.trunk_id = t.id
LEFT JOIN trunk_source_cidrs c ON c.trunk_id = t.id
LEFT JOIN trunk_auth_users a ON a.trunk_id = t.id
LEFT JOIN routing_rules r ON r.trunk_id = t.id
WHERE o.is_active = TRUE
GROUP BY t.id, t.org_id, o.slug, o.display_name, t.provider, t.stt_engine, t.languages_csv, t.require_tls, t.is_active
ORDER BY t.is_active DESC, o.slug, t.id;
```

### Verify generic inbound endpoint exists

```sql
SELECT id, context, aors, media_encryption, media_encryption_optimistic
FROM ps_endpoints
WHERE id = 'anonymous';
```

### Verify ingress hosts

```sql
SELECT trunk_id, host, is_active
FROM trunk_ingress_hosts
ORDER BY trunk_id, host;
```

### Verify route selection inputs manually

```sql
SELECT resolve_inbound_route(
  'testcustomer.sip.agentvoiceruntime.com',
  '+49123456789',
  '',
  '54.172.60.3',
  'tls'
);
```

## 11. Operational Notes For Backend Teams

- changing `require_tls` only affects signaling admission policy
- SRTP is negotiated at media level and is not currently modeled per trunk in DB
- backend systems should think of this design as:
  - DB owns trunk identity and routing policy
  - Asterisk owns transport/media negotiation and call execution
  - ARI/app layer owns call orchestration and transcription
- tenant = `organizations`
- one tenant can have multiple trunks
- runtime payload returns UUID strings for both `tenant_id` and `trunk_id`

## 12. File References

- SQL schema: `deploy/sql/config_schema.sql`
- generic inbound endpoint seed: `deploy/sql/generic_inbound_endpoint.sql`
- dialplan precheck consumer: `deploy/asterisk/extensions.conf`
- ODBC function mapping: `deploy/asterisk/func_odbc.conf`
