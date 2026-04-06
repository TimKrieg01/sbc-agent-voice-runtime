# VM Setup Required for Backend Provisioning

This runbook converts the VM requirements into executable steps and defines the JSON contract used by the backend worker.

## 0. Current behavior lock

Provisioning is ingress-host based only.
The worker must provide routing state keyed by trunk + ingress host.
Do not rely on called-number matching in this phase.

## 1. Service account and directories

Run on VM as root/admin:

```bash
sudo useradd --system --create-home --shell /bin/bash agentvoice || true
sudo mkdir -p /opt/agentvoice/bin /opt/agentvoice/state /opt/agentvoice/generated /opt/agentvoice/backups /var/log/agentvoice
sudo chown -R agentvoice:agentvoice /opt/agentvoice /var/log/agentvoice
sudo chmod 750 /opt/agentvoice/bin
sudo find /opt/agentvoice/state /opt/agentvoice/generated /opt/agentvoice/backups -type d -exec chmod 750 {} \;
```

File mode targets:
- scripts: `750`
- state/generated JSON and config files: `640`

## 2. Dependencies

```bash
sudo apt update
sudo apt install -y jq rsync
# Optional, depending on script extensions:
sudo apt install -y python3
```

Validate Asterisk CLI:

```bash
asterisk -rx "core show version"
```

## 3. Install scripts from this repo

Copy these templates to VM:
- `provisioning/bin/apply_trunk.sh`
- `provisioning/bin/verify_trunk.sh`

Install:

```bash
sudo cp provisioning/bin/apply_trunk.sh /opt/agentvoice/bin/apply_trunk.sh
sudo cp provisioning/bin/verify_trunk.sh /opt/agentvoice/bin/verify_trunk.sh
sudo chown agentvoice:agentvoice /opt/agentvoice/bin/apply_trunk.sh /opt/agentvoice/bin/verify_trunk.sh
sudo chmod 750 /opt/agentvoice/bin/apply_trunk.sh /opt/agentvoice/bin/verify_trunk.sh
```

## 4. SSH access for worker

```bash
sudo -u agentvoice mkdir -p /home/agentvoice/.ssh
sudo -u agentvoice chmod 700 /home/agentvoice/.ssh
sudo -u agentvoice touch /home/agentvoice/.ssh/authorized_keys
sudo -u agentvoice chmod 600 /home/agentvoice/.ssh/authorized_keys
```

Add backend worker public key to `/home/agentvoice/.ssh/authorized_keys`.

Security requirements:
- restrict source IP/VNet to backend worker only,
- disable password login in SSH daemon,
- publish VM host key fingerprint to backend team.

## 5. Sudo policy (restricted)

`agentvoice` should not have general interactive sudo.
Only allow explicit Asterisk reload/status commands if required by your setup.

Example `sudoers` snippet (adjust paths/commands):

```text
agentvoice ALL=(root) NOPASSWD: /usr/sbin/asterisk -rx pjsip\ reload
agentvoice ALL=(root) NOPASSWD: /usr/sbin/asterisk -rx core\ show\ version
agentvoice ALL=(root) NOPASSWD: /usr/sbin/asterisk -rx core\ show\ channels\ count
agentvoice ALL=(root) NOPASSWD: /usr/sbin/asterisk -rx pjsip\ show\ endpoint\ *
```

## 6. JSON contract expected by scripts

`apply_trunk.sh` and `verify_trunk.sh` expect a state file with at least:

```json
{
  "trunk_id": "org1-main",
  "ingress_hosts": ["org1.sip.voiceagentruntime.com"],
  "auth_users": ["org1-auth"],
  "routing_rules": [
    { "priority": 100, "backend_url": "https://backend.example.org1/ingest" }
  ],
  "acl": {
    "signaling_cidrs": ["54.172.60.0/23"],
    "media_cidrs": ["54.172.60.0/23"]
  },
  "transport": {
    "allowed_transports": ["tls", "udp"],
    "signaling_ports": [5061, 5060],
    "tls_enabled": true,
    "rtp": { "port_start": 10000, "port_end": 20000 }
  },
  "requires_disruptive_action": false
}
```

### 6.1 State file specification (exact contract for worker team)

Top-level object:
- must be valid JSON object
- UTF-8 text file
- absolute path on VM (script rejects relative path)

Required fields:
1. `ingress_hosts`
- type: `array[string]`
- min items: `0` (recommended `>=1`)
- example: `["org1.sip.voiceagentruntime.com"]`

2. `auth_users`
- type: `array[string]`
- example: `["org1-auth"]`

3. `routing_rules`
- type: `array[object]`
- each rule should include at least:
  - `priority` (number)
  - `backend_url` (string, https URL)

4. `acl.signaling_cidrs`
- type: `array[string]`
- CIDR strings used for SIP signaling source matching

5. `acl.media_cidrs`
- type: `array[string]`
- CIDR strings used for RTP media source policy

6. `transport.allowed_transports`
- type: `array[string]`
- allowed values: `udp`, `tcp`, `tls`
- min items: `1`

7. `transport.signaling_ports`
- type: `array[number]`
- min items: `1`
- typical values: `5060`, `5061`

8. `transport.rtp.port_start`
- type: `number`

9. `transport.rtp.port_end`
- type: `number`
- must satisfy `port_start <= port_end`

Optional fields:
1. `trunk_id`
- type: `string`
- if present, must match `--trunk-id` arg

2. `transport.tls_enabled`
- type: `boolean`
- default used by script: `false`

3. `requires_disruptive_action`
- type: `boolean`
- if `true`, script checks active channels and may return `deferred_active_calls`

### 6.2 Minimum valid payload (smallest accepted shape)

```json
{
  "ingress_hosts": ["org1.sip.voiceagentruntime.com"],
  "auth_users": [],
  "routing_rules": [
    { "priority": 100, "backend_url": "https://some.example.com" }
  ],
  "acl": {
    "signaling_cidrs": ["54.172.60.0/23"],
    "media_cidrs": ["54.172.60.0/23"]
  },
  "transport": {
    "allowed_transports": ["tls"],
    "signaling_ports": [5061],
    "rtp": { "port_start": 10000, "port_end": 20000 }
  }
}
```

### 6.3 Recommended production payload

```json
{
  "trunk_id": "org1-main",
  "ingress_hosts": ["org1.sip.voiceagentruntime.com"],
  "auth_users": ["org1-auth"],
  "routing_rules": [
    {
      "priority": 100,
      "backend_url": "https://some.example.com",
      "path": "/sip-events",
      "enabled": true
    }
  ],
  "acl": {
    "signaling_cidrs": ["54.172.60.0/23", "34.203.250.0/23"],
    "media_cidrs": ["54.172.60.0/23", "34.203.250.0/23"]
  },
  "transport": {
    "allowed_transports": ["tls", "udp"],
    "signaling_ports": [5061, 5060],
    "tls_enabled": true,
    "rtp": { "port_start": 10000, "port_end": 20000 }
  },
  "requires_disruptive_action": false
}
```

### 6.4 Worker team handoff checklist

Give the provisioning worker team:
1. Absolute VM script paths:
- `/opt/agentvoice/bin/apply_trunk.sh`
- `/opt/agentvoice/bin/verify_trunk.sh`
2. This JSON structure contract (Section 6.1).
3. One production-like example payload per tenant/trunk (Section 6.3).
4. The exact SSH trigger sequence from Section 8.1.
5. Expected exit behavior:
- exit `0` = success JSON
- non-zero = failure JSON (parse `code` and `message`)

## 7. Runtime behavior of templates

`/opt/agentvoice/bin/apply_trunk.sh`:
- validates args and JSON schema,
- renders candidate files (ingress/auth/routing/acl/transport + PJSIP fragment),
- backs up current active files,
- atomically swaps active files,
- runs non-disruptive `pjsip reload`,
- verifies endpoint visibility,
- rolls back on failure,
- outputs machine-readable JSON to stdout.

`/opt/agentvoice/bin/verify_trunk.sh`:
- validates args and JSON,
- compares desired state with active normalized state,
- verifies active files exist,
- checks runtime endpoint visibility,
- outputs verification JSON.

## 8. Example invocations

```bash
/opt/agentvoice/bin/apply_trunk.sh \
  --trunk-id org1-main \
  --state-file /opt/agentvoice/state/org1-main.json
```

```bash
/opt/agentvoice/bin/verify_trunk.sh \
  --trunk-id org1-main \
  --state-file /opt/agentvoice/state/org1-main.json
```

## 8.1 Worker trigger contract (SSH)

Expected input params:
1. `--trunk-id <id>`
2. `--state-file <absolute-json-path>`

Trigger sequence:
1. Upload JSON state file to VM:

```bash
scp trunk_state.json agentvoice@<VM_HOST>:/opt/agentvoice/state/org1-main.json
```

2. Apply:

```bash
ssh agentvoice@<VM_HOST> "/opt/agentvoice/bin/apply_trunk.sh --trunk-id org1-main --state-file /opt/agentvoice/state/org1-main.json"
```

3. Verify:

```bash
ssh agentvoice@<VM_HOST> "/opt/agentvoice/bin/verify_trunk.sh --trunk-id org1-main --state-file /opt/agentvoice/state/org1-main.json"
```

The worker must parse stdout JSON and treat non-zero exit as failure.

## 9. Example success output

```json
{
  "script": "apply_trunk.sh",
  "version": "1.0.0",
  "timestamp": "20260406T120000Z",
  "trunk_id": "org1-main",
  "status": "succeeded",
  "code": "ok",
  "message": "Trunk applied successfully",
  "candidate_dir": "/opt/agentvoice/state/candidates/org1-main-20260406T120000Z",
  "backup_path": "/opt/agentvoice/backups/org1-main/20260406T120000Z",
  "active_path": "/opt/agentvoice/generated/active/trunks/org1-main"
}
```

## 10. Example failure outputs

Deferred because active calls would be disrupted:

```json
{
  "script": "apply_trunk.sh",
  "version": "1.0.0",
  "timestamp": "20260406T120030Z",
  "trunk_id": "org1-main",
  "status": "failed",
  "code": "deferred_active_calls",
  "message": "Disruptive action blocked while calls are active",
  "active_channels": 3
}
```

Verification mismatch:

```json
{
  "script": "verify_trunk.sh",
  "version": "1.0.0",
  "timestamp": "20260406T120100Z",
  "trunk_id": "org1-main",
  "status": "failed",
  "code": "state_mismatch",
  "message": "Active state does not match desired state"
}
```

## 11. Logging

Structured JSON log lines are written to:
- `/var/log/agentvoice/provisioning.log`

Each entry includes:
- `timestamp`
- `script`
- `job_id`
- `trunk_id`
- `action`
- `result`
- `details`

## 12. Handoff back to backend team

Provide:
1. VM host/IP and SSH user (`agentvoice`)
2. VM SSH host key fingerprint
3. Absolute script paths:
- `/opt/agentvoice/bin/apply_trunk.sh`
- `/opt/agentvoice/bin/verify_trunk.sh`
4. Sample success/failure outputs from both scripts
5. Any VM-specific environment variable overrides if used

## 13. Important integration note

These scripts are safe templates and may need adaptation to your exact Asterisk include layout (`/etc/asterisk/*.conf` vs generated include paths). Keep non-disruptive reload as the default and avoid service restarts in normal provisioning flow.

## 14. VM ready-now checklist (after git pull)

Run this on VM:

```bash
cd /home/azureuser/agentic-sip-trunk
git pull
sudo cp provisioning/bin/apply_trunk.sh /opt/agentvoice/bin/apply_trunk.sh
sudo cp provisioning/bin/verify_trunk.sh /opt/agentvoice/bin/verify_trunk.sh
sudo chown agentvoice:agentvoice /opt/agentvoice/bin/apply_trunk.sh /opt/agentvoice/bin/verify_trunk.sh
sudo chmod 750 /opt/agentvoice/bin/apply_trunk.sh /opt/agentvoice/bin/verify_trunk.sh
sudo systemctl enable asterisk
sudo systemctl start asterisk
sudo systemctl status asterisk --no-pager
asterisk -rx "core show version"
```

If you run app services on the VM, also ensure:

```bash
sudo systemctl enable agentic-app agentic-ari-bridge
sudo systemctl restart agentic-app agentic-ari-bridge
sudo systemctl status agentic-app agentic-ari-bridge --no-pager
```

## 15. Inspect incoming SIP traffic and provisioning logs

Provisioning script logs:

```bash
sudo tail -f /var/log/agentvoice/provisioning.log
```

Asterisk live console:

```bash
sudo asterisk -rvvv
```

Inside Asterisk console for SIP signaling visibility:

```text
pjsip set logger on
```

For RTP packet debug (temporary):

```text
rtp set debug on
```

Service logs:

```bash
journalctl -u asterisk -f
journalctl -u agentic-app -f
journalctl -u agentic-ari-bridge -f
```
