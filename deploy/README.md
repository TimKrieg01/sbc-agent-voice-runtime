# Minimal Azure VM Deployment (Asterisk + Python Media Service)

This repo now contains a minimal real SIP edge path:

- Asterisk terminates SIP/RTP from Twilio
- ARI worker creates an `externalMedia` channel per call
- Python app receives RTP PCMU via local UDP and feeds existing orchestrator/STT/turn logic

## 1) Install runtime on VM (Ubuntu 22.04)

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

- `5060/udp` SIP signaling (or `5061/tcp` for TLS)
- `10000-20000/udp` RTP media
- `22/tcp` SSH restricted to your source IP

## 6) Twilio trunk (inbound first)

- Configure Twilio Elastic SIP trunk Origination URI to your VM public SIP address.
- Ensure number is attached to the trunk.
- Make a test call and inspect logs:

```bash
sudo asterisk -rvvv
journalctl -u agentic-app -f
journalctl -u agentic-ari-bridge -f
```

## Notes on current scope

- This is intentionally minimal and optimized for first-call success.
- RTP ingest into Python is implemented.
- Streaming synthesized TTS back into call media is not wired yet in this commit.
