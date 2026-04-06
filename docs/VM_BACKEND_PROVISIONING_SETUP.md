# VM Setup Required for Backend Provisioning

This runbook converts the VM requirements into executable steps and defines the JSON contract used by the backend worker.

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
