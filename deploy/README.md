# Stateless VM Deployment (Asterisk Sorcery + Realtime DB)

This repository now targets a stateless SBC runtime:
- No generated per-trunk `.conf` files on VM.
- Database is the runtime source of truth for trunk admission and route decisions.
- Asterisk uses Sorcery realtime (`ps_*` tables) and ODBC precheck (`resolve_inbound_route`).

## 1. Target Runtime Model

Call flow:
1. SIP INVITE arrives at Asterisk.
2. PJSIP admits the request through the generic anonymous endpoint.
3. Call enters `inbound-realtime` dialplan.
4. Dialplan calls `ODBC_AV_ROUTE_DECIDE(...)` which calls DB function `resolve_inbound_route(...)`.
5. If `reject`, hangup before `Answer()`.
6. If `allow`, pass route metadata into `Stasis(...)` and continue ARI media flow.

This enforces: "only routeable calls proceed".

## 2. VM Prerequisites

Ubuntu 22.04/24.04 recommended.

```bash
sudo apt update
sudo apt install -y \
  asterisk \
  asterisk-odbc \
  unixodbc \
  odbc-postgresql \
  jq \
  git \
  python3 \
  python3-venv \
  python3-pip \
  certbot
```

## 3. Clone and Python Runtime

```bash
cd /home/azureuser
git clone <YOUR_REPO_URL> agentic-sip-trunk
cd agentic-sip-trunk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Populate `.env` at minimum:
- `AZURE_SPEECH_KEY`
- `AZURE_SPEECH_REGION`
- `ASTERISK_ARI_*`
- `PYTHON_APP_BASE`
- `SIP_CONFIG_DATABASE_URL`

## 3.1 Create VM-Specific Render Variables

Copy the VM template vars file and fill in your local values:

```bash
cp deploy/vm.env.example deploy/vm.env
nano deploy/vm.env
```

Use it to render the VM-specific config files:

```bash
python3 scripts/render_vm_configs.py
```

This writes generated files under `deploy/rendered/`.

## 4. Initialize Database Schema

Run `deploy/sql/config_schema.sql` against your target PostgreSQL instance (for example Azure Database for PostgreSQL).

Example with direct connection string:

```bash
psql "host=<DB_HOST> port=5432 dbname=<DB_NAME> user=<DB_USER> password=<DB_PASSWORD> sslmode=require" \
  -v ON_ERROR_STOP=1 \
  -f deploy/sql/config_schema.sql
```

Example with `PGPASSWORD`:

```bash
export PGPASSWORD="<DB_PASSWORD>"
psql -h <DB_HOST> -p 5432 -U <DB_USER> -d <DB_NAME> \
  -v ON_ERROR_STOP=1 \
  -f deploy/sql/config_schema.sql
```

This creates:
- business policy tables (organizations, trunks, ingress hosts, auth users, CIDRs, routing rules)
- Sorcery realtime tables (`ps_endpoints`, `ps_aors`, `ps_auths`, `ps_endpoint_id_ips`, ...)
- routing decision function `resolve_inbound_route(...)`

## 5. Configure ODBC DSN on VM

Create `/etc/odbc.ini` (example):

```ini
[asterisk_cfg]
Driver=PostgreSQL Unicode
Servername=<DB_HOST>
Port=5432
Database=<DB_NAME>
Username=<DB_USER>
Password=<DB_PASSWORD>
SSLmode=require
```

Check:

```bash
isql -v asterisk_cfg <DB_USER> <DB_PASSWORD>
```

## 6. Install Asterisk Config Files

```bash
python3 scripts/render_vm_configs.py
sudo cp deploy/rendered/asterisk/pjsip.conf /etc/asterisk/pjsip.conf
sudo cp deploy/asterisk/extensions.conf /etc/asterisk/extensions.conf
sudo cp deploy/rendered/asterisk/http.conf /etc/asterisk/http.conf
sudo cp deploy/rendered/asterisk/ari.conf /etc/asterisk/ari.conf
sudo cp deploy/rendered/asterisk/rtp.conf /etc/asterisk/rtp.conf
sudo cp deploy/asterisk/sorcery.conf /etc/asterisk/sorcery.conf
sudo cp deploy/asterisk/extconfig.conf /etc/asterisk/extconfig.conf
sudo cp deploy/rendered/asterisk/res_odbc.conf /etc/asterisk/res_odbc.conf
sudo cp deploy/asterisk/func_odbc.conf /etc/asterisk/func_odbc.conf
```

## 7. TLS Certificates (Recommended Simple Path)

Use Let's Encrypt for SIP FQDN (for example `sip.agentvoiceruntime.com`):

```bash
sudo systemctl stop asterisk
sudo certbot certonly --standalone -d sip.agentvoiceruntime.com
sudo systemctl start asterisk
```

Install cert/key for Asterisk transport:

```bash
sudo mkdir -p /etc/asterisk/keys
sudo install -m 640 -o root -g asterisk \
  /etc/letsencrypt/live/sip.agentvoiceruntime.com/fullchain.pem \
  /etc/asterisk/keys/fullchain.pem
sudo install -m 640 -o root -g asterisk \
  /etc/letsencrypt/live/sip.agentvoiceruntime.com/privkey.pem \
  /etc/asterisk/keys/privkey.pem
```

Renewal hook `/etc/letsencrypt/renewal-hooks/deploy/asterisk-reload.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
DOMAIN="sip.agentvoiceruntime.com"
install -m 640 -o root -g asterisk "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" /etc/asterisk/keys/fullchain.pem
install -m 640 -o root -g asterisk "/etc/letsencrypt/live/${DOMAIN}/privkey.pem" /etc/asterisk/keys/privkey.pem
asterisk -rx "pjsip reload"
```

## 8. Start Asterisk and Verify Realtime Wiring

```bash
sudo systemctl enable asterisk
sudo systemctl restart asterisk
sudo systemctl status asterisk --no-pager
sudo asterisk -rx "odbc show"
sudo asterisk -rx "pjsip show endpoints"
sudo asterisk -rx "dialplan show inbound-realtime"
```

If `pjsip show endpoints` is empty, your `ps_*` rows are not populated yet.

## 8.1 Seed First Trunk (Mandatory)

Before traffic can be accepted, you must insert at least:
- one active `organization`
- one active `inbound_trunk`
- one active `trunk_ingress_host`
- zero or more active `trunk_source_cidr` rows when source IP allowlisting is desired
- one active `routing_rule`
- the generic inbound `ps_endpoints(id='anonymous')` + `ps_aors(id='anonymous')` rows

Optional per-trunk signaling security:
- set `inbound_trunks.require_tls = TRUE` to reject non-TLS signaling for that trunk
- RTP/SRTP remains transport-neutral in the generic inbound model

Then run:

```bash
psql "host=<DB_HOST> port=5432 dbname=<DB_NAME> user=<DB_USER> password=<DB_PASSWORD> sslmode=require" \
  -v ON_ERROR_STOP=1 \
  -f deploy/sql/generic_inbound_endpoint.sql
sudo asterisk -rx "pjsip reload"
```

The generic inbound endpoint is currently transport-neutral.
If you want TLS/SRTP requirements to vary by trunk, do not pin those settings globally on `anonymous`; model them separately as trunk policy first.

## 9. Install App Services

```bash
python3 scripts/render_vm_configs.py
sudo cp deploy/rendered/systemd/agentic-app.service /etc/systemd/system/agentic-app.service
sudo cp deploy/rendered/systemd/agentic-ari-bridge.service /etc/systemd/system/agentic-ari-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable agentic-app agentic-ari-bridge
sudo systemctl restart agentic-app agentic-ari-bridge
sudo systemctl status agentic-app agentic-ari-bridge --no-pager
```

## 9.1 After Every `git pull`

Use this workflow instead of manual file edits:

```bash
cd ~/agentic-sip-trunk
git pull
python3 scripts/render_vm_configs.py
sudo cp deploy/rendered/asterisk/pjsip.conf /etc/asterisk/pjsip.conf
sudo cp deploy/rendered/asterisk/http.conf /etc/asterisk/http.conf
sudo cp deploy/rendered/asterisk/ari.conf /etc/asterisk/ari.conf
sudo cp deploy/rendered/asterisk/rtp.conf /etc/asterisk/rtp.conf
sudo cp deploy/rendered/asterisk/res_odbc.conf /etc/asterisk/res_odbc.conf
sudo cp deploy/asterisk/extensions.conf /etc/asterisk/extensions.conf
sudo cp deploy/asterisk/extconfig.conf /etc/asterisk/extconfig.conf
sudo cp deploy/asterisk/sorcery.conf /etc/asterisk/sorcery.conf
sudo cp deploy/asterisk/func_odbc.conf /etc/asterisk/func_odbc.conf
sudo cp deploy/rendered/systemd/agentic-app.service /etc/systemd/system/agentic-app.service
sudo cp deploy/rendered/systemd/agentic-ari-bridge.service /etc/systemd/system/agentic-ari-bridge.service
sudo systemctl daemon-reload
sudo systemctl restart asterisk agentic-app agentic-ari-bridge
```

## 10. Required Network Rules

- `5061/tcp` (SIP TLS)
- `5060/udp` only if UDP SIP is required
- `10000-20000/udp` RTP
- `22/tcp` SSH restricted to admin source IPs

## 11. Runtime Observability

```bash
sudo asterisk -rvvv
pjsip set logger on
journalctl -u asterisk -f
journalctl -u agentic-app -f
journalctl -u agentic-ari-bridge -f
```

## 12. Handoff Docs

- DB schema and backend contract: `docs/DB_SCHEMA_REALTIME_HANDOFF.md`
- Runtime architecture for all teams: `docs/VM_RUNTIME_SORCERY_OVERVIEW.md`

## 13. Explicitly Removed in This Model

- Per-trunk generated config lifecycle on VM as a routing dependency
- File-based trunk apply/verify/delete runtime flow

All runtime admission and route selection now come from DB + Sorcery/ODBC.
