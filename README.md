# Agentic SIP Trunk Runtime

Stateless SIP ingress runtime for Asterisk + ARI with database-driven trunk admission and routing.

## Canonical Docs

- VM installation runbook: `deploy/README.md`
- Database schema and backend contract: `docs/DB_SCHEMA_REALTIME_HANDOFF.md`
- Runtime architecture overview for all teams: `docs/VM_RUNTIME_SORCERY_OVERVIEW.md`

## Design Summary

- No generated per-trunk `.conf` files on VM.
- Asterisk PJSIP objects are loaded from realtime DB tables via Sorcery (`ps_*`).
- Dialplan precheck calls DB function `resolve_inbound_route(...)` via ODBC.
- Calls are rejected before Stasis when host/auth/route checks fail, with CIDR checks applied only when configured for that trunk.
- ARI worker only handles calls that already passed route admission.

## Quick Start

1. Clone the repo on the VM and create Python venv.
2. Configure `.env` from `.env.example`.
3. Apply `deploy/sql/config_schema.sql` to PostgreSQL.
4. Copy Asterisk configs from `deploy/asterisk/` to `/etc/asterisk/`.
5. Install systemd services from `deploy/systemd/`.
6. Restart Asterisk and app services and run verification commands from `deploy/README.md`.
