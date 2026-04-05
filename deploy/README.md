# Minimal Azure VM Deployment (Asterisk + Python Media Service)

This repo now contains a minimal real SIP edge path:

- Asterisk terminates SIP/RTP from Twilio
- ARI worker creates an `externalMedia` channel per call
- Python app receives RTP PCMU via local UDP and feeds existing orchestrator/STT/turn logic

## 1) Install runtime on VM (Ubuntu 22.04/24.04)

```bash
sudo apt update
sudo apt install -y asterisk python3-venv python3-pip git
```

## 2) Clone repo + Python env

```bash
cd /home/azureuser
git clone <YOUR_REPO_URL> agentic-sip-trunk
cd agentic-sip-trunk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set your Azure speech credentials in `.env`.

## 3) Copy Asterisk config

```bash
sudo cp deploy/asterisk/pjsip.conf /etc/asterisk/pjsip.conf
sudo cp deploy/asterisk/extensions.conf /etc/asterisk/extensions.conf
sudo cp deploy/asterisk/http.conf /etc/asterisk/http.conf
sudo cp deploy/asterisk/ari.conf /etc/asterisk/ari.conf
sudo cp deploy/asterisk/rtp.conf /etc/asterisk/rtp.conf
```

Important:
- In `pjsip.conf`, set `external_signaling_address` and `external_media_address`
  to your VM public IP/FQDN.
- Update Twilio IP matches in `pjsip.conf` for your region.
- For production, switch to TLS/SRTP and tighten access controls.

Restart Asterisk:

```bash
sudo systemctl enable asterisk
sudo systemctl restart asterisk
sudo systemctl status asterisk
```

## 4) Install systemd services

```bash
sudo cp deploy/systemd/agentic-app.service /etc/systemd/system/agentic-app.service
sudo cp deploy/systemd/agentic-ari-bridge.service /etc/systemd/system/agentic-ari-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable agentic-app
sudo systemctl enable agentic-ari-bridge
sudo systemctl start agentic-app
sudo systemctl start agentic-ari-bridge
sudo systemctl status agentic-app
sudo systemctl status agentic-ari-bridge
```

If your VM username/path differs from `azureuser`, update both service files first.

## 5) Azure NSG/firewall ports

- `5060/udp` SIP signaling (legacy/transition)
- `5061/tcp` SIP over TLS (recommended)
- `10000-20000/udp` RTP media
- `22/tcp` SSH restricted to your source IP

## 6) Dialplan matching for Twilio DID format

Twilio commonly sends destination numbers in E.164 format (`+123...`).
`deploy/asterisk/extensions.conf` includes both `_+X!` and `_X!` patterns.

## 7) Twilio trunk (inbound first)

- Configure Twilio Elastic SIP trunk Origination URI to your VM public SIP address.
- Ensure number is attached to the trunk.
- Make a test call and inspect logs:

```bash
sudo asterisk -rvvv
journalctl -u agentic-app -f
journalctl -u agentic-ari-bridge -f
```

## 7.1) Enable TLS Secure SIP

1. Place your TLS cert and key on the VM:

```bash
sudo mkdir -p /etc/asterisk/keys
sudo cp /path/to/fullchain.pem /etc/asterisk/keys/fullchain.pem
sudo cp /path/to/privkey.pem /etc/asterisk/keys/privkey.pem
sudo chown asterisk:asterisk /etc/asterisk/keys/fullchain.pem /etc/asterisk/keys/privkey.pem
sudo chmod 640 /etc/asterisk/keys/fullchain.pem /etc/asterisk/keys/privkey.pem
```

2. In `deploy/asterisk/pjsip.conf`, set:
- `external_signaling_address=<PUBLIC_FQDN>`
- `external_media_address=<PUBLIC_FQDN>`
- `transport-tls` cert paths if different from defaults

3. Ensure Azure NSG allows `5061/tcp` from Twilio SIP signaling ranges.

4. Reload/restart Asterisk:

```bash
sudo asterisk -rx "pjsip reload"
sudo systemctl restart asterisk
```

5. In Twilio Elastic SIP Trunk, set Origination URI to:
- `sip:<number>@<tenant>.sip.agentvoiceruntime.com:5061;transport=tls`

6. Verify in Asterisk CLI:

```bash
sudo asterisk -rvvv
pjsip show transports
pjsip set logger on
```

You should see inbound INVITEs on `TCP/TLS` to port `5061`.

## 7.2) Enable Secure RTP (optional hardening)

When Twilio Secure Trunking SRTP is enabled, update endpoint media encryption:

```ini
; /etc/asterisk/pjsip.conf
[twilio-inbound]
media_encryption=sdes
media_use_received_transport=yes
```

Then reload and test. If calls fail after enabling SRTP, revert these two lines and confirm
Twilio trunk SRTP settings are aligned first.

## 8) Multi-tenant SIP routing (shared infra)

The ARI bridge now resolves tenant per call by ingress host before media starts.

1. Inbound dialplan passes these into `Stasis` args:
- dialed number (`${EXTEN}`)
- `To` header (`${PJSIP_HEADER(read,To)}`), used to parse ingress host
- optional auth hint header

2. Configure tenant rules in `.env`:

```bash
SIP_STRICT_TENANT_RESOLUTION=true
SIP_TENANT_RULES_JSON=[{"tenant_id":"acme","trunk_id":"acme-main","ingress_hosts":["acme.sip.agentvoiceruntime.com"],"called_numbers":["+49123456789"],"auth_users":["acme-auth"],"stt_engine":"azure","languages":["en-US","de-DE"]}]
```

Notes:
- `ingress_hosts` is required for tenant identity.
- `auth_users` is optional and only used as extra validation for that matched host.
- `called_numbers` is not used to identify tenant by itself.

3. For each customer, onboard a unique host under your wildcard DNS:
- `customer1.sip.agentvoiceruntime.com`
- `customer2.sip.agentvoiceruntime.com`

4. If a call is unknown or ambiguous and strict mode is enabled, the bridge rejects it.

## Production baseline practices

- Keep Asterisk and Python app as separate `systemd` services on the same VM.
- Pin and deploy from tagged releases (`vX.Y.Z`), not ad-hoc local copies.
- Store secrets in a managed secret store (Azure Key Vault) and inject at deploy.
- Restrict NSG source ranges from `Any` to Twilio SIP/media ranges and trusted admin IPs.
- Add health checks and alerting:
  - `systemctl is-active asterisk agentic-app agentic-ari-bridge`
  - log shipping from `journalctl` and `/var/log/asterisk`.
- Use a second VM for blue/green updates before production cutover.

## Notes on current scope

- This is intentionally minimal and optimized for first-call success.
- RTP ingest into Python is implemented.
- Streaming synthesized TTS back into call media is not wired yet in this commit.
