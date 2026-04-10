# VM Runtime Overview: Sorcery + Realtime Routing

This document explains how the VM works in the stateless design.

## 1. Design Intent

- Runtime decisions come from DB, not generated config files.
- Asterisk rejects non-routeable calls before acceptance.
- ARI worker handles media only after precheck allows route.

## 2. Components

1. Asterisk PJSIP
- SIP ingress and endpoint identification
- Realtime endpoint objects from Sorcery (`ps_*` tables)

2. Asterisk Dialplan
- `inbound-realtime` context
- Calls ODBC precheck function `ODBC_AV_ROUTE_DECIDE(...)`
- Rejects early if route is unavailable

3. ODBC / DB Function
- `resolve_inbound_route(...)`
- Returns allow/reject payload with route metadata

4. ARI Bridge Worker (`src/services/sip/ari_bridge.py`)
- Consumes Stasis args produced by precheck
- Starts RTP media session only for pre-approved calls

5. FastAPI SIP internal API (`src/api/sip_routes.py`)
- Session lifecycle for RTP ingest/orchestrator

## 3. Runtime Call Sequence

1. INVITE arrives.
2. PJSIP identifies endpoint from realtime DB, preferring the called host/header.
3. Dialplan extracts host/auth/source/called.
4. Dialplan calls `ODBC_AV_ROUTE_DECIDE`.
5. If `reject` -> immediate hangup with cause code.
6. If `allow` -> `Stasis(agentic, ..., trunk_id, backend_url, route_id, tenant_id, stt_engine, languages)`.
7. ARI worker bridges media to internal RTP consumer.

Implementation files:
- Dialplan precheck: `deploy/asterisk/extensions.conf`
- ODBC call mapping: `deploy/asterisk/func_odbc.conf`
- Realtime mappings: `deploy/asterisk/sorcery.conf`, `deploy/asterisk/extconfig.conf`
- ARI worker consumer: `src/services/sip/ari_bridge.py`

## 4. Admission Guarantees

This model enforces:
- unknown host rejected
- source IP not in CIDR rejected
- auth user mismatch rejected (when configured)
- source CIDR mismatch rejected only when CIDR allowlists are configured for the trunk
- no matching route rejected

No route validation is deferred to post-answer business logic.

## 5. Static Files Still Needed

Only bootstrap files remain static on VM:
- `pjsip.conf` (global + transports)
- `extensions.conf` (generic realtime precheck)
- `sorcery.conf`, `extconfig.conf`, `res_odbc.conf`, `func_odbc.conf`
- `http.conf`, `ari.conf`, `rtp.conf`

No static per-trunk files are needed.

## 6. Operational Reload Model

- DB changes become active after:
  - `pjsip reload` for Sorcery PJSIP object changes
  - `dialplan reload` only when dialplan templates change

Normal trunk onboarding should only require DB writes + `pjsip reload`.

## 6.1 Minimal VM State

The VM should not hold trunk business state as generated files.
Only bootstrap config and runtime services live on disk:
- Asterisk bootstrap config in `/etc/asterisk/`
- App code checkout + `.env`
- TLS keypair/cert for SIP transport

## 7. Failure Domains

- DB unavailable -> ODBC function fails -> dialplan reject with temporary failure.
- ARI worker down -> calls can enter Stasis but media session creation fails; monitor service health.
- DB policy mistakes (regex/priority) -> deterministic but potentially wrong route choice.

## 8. Observability

Primary checks:

```bash
asterisk -rx "odbc show"
asterisk -rx "pjsip show endpoints"
asterisk -rx "dialplan show inbound-realtime"
```

Logs:
- `journalctl -u asterisk -f`
- `journalctl -u agentic-ari-bridge -f`
- `journalctl -u agentic-app -f`

## 9. Team Responsibilities

- Backend team:
  - own business tables + route function correctness
  - maintain `ps_*` projection consistency
- Platform/VM team:
  - own Asterisk/ODBC/Sorcery bootstrap and TLS lifecycle
- Voice/AI app team:
  - own ARI media handling after route approval
