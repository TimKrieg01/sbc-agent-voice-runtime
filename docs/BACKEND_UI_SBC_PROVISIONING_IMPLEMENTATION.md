# Backend Implementation: Everything Needed Now for UI -> SBC + NSG

This document is the backend execution contract to make UI changes become real, safe, live SBC behavior and network access control.

## 0. Scope lock for now: ingress-host only routing

For the current phase, trunk/tenant selection is based only on `ingress_hosts`.

Explicitly out of scope for now:
- called-number based trunk selection,
- host+number combined matching logic.

Routing model for now:
1. Match inbound call by ingress host (for example `org1.sip.agentvoiceruntime.com`).
2. Resolve trunk/org from that host.
3. Forward to configured backend URL from trunk routing rules.

## 1. Final Architecture (must be implemented)

Three planes:
1. Control plane (API + DB)
- Stores desired config from UI.
- Enqueues provisioning jobs after successful writes.

2. Provisioning plane (worker service)
- Runs asynchronously.
- Applies config to SBC/Asterisk VM and Azure NSG.
- Verifies, retries, updates sync status and audit.

3. Runtime plane (call processing on VM)
- Handles live SIP calls.
- Must remain lightweight.
- Must not do heavy provisioning work during call setup.

## 2. Clear answer to runtime behavior

What happens on new calls:
- Runtime may read tenant mapping from DB/realtime/cached config.
- It should not run full provisioning workflows per call.

What the backend must do:
- Push/apply provisioning ahead of time via worker.
- Keep runtime lookup fast and deterministic.

## 3. What backend must build now (complete checklist)

1. Add provisioning job system
- Durable queue/outbox.
- Per-scope serialization (`trunk:{id}`, `global:nsg`, `global:provider-cidrs`).
- Retry/backoff + dead-letter behavior.

2. Wire all config-write endpoints to enqueue jobs
- After DB transaction commit only.
- Do not block UI requests on external SBC/NSG apply.

3. Implement SBC apply adapter
- Mechanism: SSH/agent/API from worker to VM.
- Render deterministic config artifacts from DB state.
- Apply with safe reload strategy (no disruptive restart in normal flow).

4. Implement NSG apply adapter
- Azure API integration for inbound allow rules.
- Manage SIP + RTP + required admin ports with least privilege.
- Enforce deterministic rule naming and priorities.

5. Implement sync status model + endpoint wiring
- `pending`, `in_sync`, `failed`, `drifted`.
- `/security/policy-sync-status` must report real job outcomes.

6. Implement audit/observability
- Record actor, entity, diff, external correlation IDs, result, errors.
- Metrics for latency, success rate, retries, drift.

7. Implement reconciliation loop
- Periodically compare desired vs actual SBC + NSG state.
- Auto-heal drift and mark status accordingly.

8. Implement runtime-safe reload rules
- Targeted reload only.
- Defer disruptive actions while active channels exist.
- Health-check and rollback on failed apply.

## 4. Trigger -> work mapping (must be exact)

1. Trunks
- `POST /orgs/{orgId}/trunks` -> create trunk config on SBC, ensure base NSG requirements.
- `PATCH /trunks/{trunkId}` -> update trunk config.
- `POST /trunks/{trunkId}/disable` -> disable inbound acceptance for new calls.
- `POST /trunks/{trunkId}/enable` -> re-enable.

2. Ingress hosts
- `POST /trunks/{trunkId}/ingress-hosts` -> add host/domain matching.
- `DELETE /trunks/{trunkId}/ingress-hosts/{host}` -> remove host mapping.

3. Trunk auth users
- `POST /trunks/{trunkId}/auth-users` -> add/update auth identity in SBC config.
- `DELETE /trunks/{trunkId}/auth-users/{authUser}` -> remove identity.

4. Routing rules
- `POST /trunks/{trunkId}/routing-rules` -> add route target.
- `PATCH /routing-rules/{ruleId}` -> update route target/priority.
- `DELETE /routing-rules/{ruleId}` -> remove route.

5. CIDRs + NSG
- `POST /trunks/{trunkId}/source-cidrs` -> apply trunk ACL + NSG allow where required.
- `DELETE /trunks/{trunkId}/source-cidrs/{cidr}` -> remove ACL + corresponding NSG allow.
- `POST /security/provider-cidrs` -> apply provider/global allow in SBC ACL + NSG.
- `DELETE /security/provider-cidrs/{cidr}` -> remove provider/global allow.

## 5. VM access and apply method (backend responsibility)

The worker must have secure runtime access to the VM:
1. Preferred
- Private network path + managed identity/service principal + short-lived credentials.

2. Accepted
- Controlled SSH with locked-down key scope and command policy.

3. Not acceptable
- Manual operator-only updates as primary mechanism.

The worker applies changes on live VM using:
1. Generate candidate config.
2. Validate syntax.
3. Atomic write/swap.
4. Targeted reload.
5. Read-back verification.
6. Mark success/failure.

## 6. Active-call safety policy (must implement)

Allowed during active calls:
- Non-disruptive reload operations that affect new calls.

Blocked/deferred during active calls:
- Service restart or any action that can drop established channels.

Required safeguards:
1. Check active channel count before disruptive step.
2. If active > 0, set job state `deferred_active_calls`.
3. Retry later or schedule maintenance window.
4. Run post-apply health checks.
5. Roll back previous known-good config on health-check failure.

## 7. NSG scope that backend must manage

Backend must own NSG rule lifecycle for:
1. SIP signaling
- UDP/TCP 5060 (if used), TLS 5061 (if used).

2. RTP media range
- Exact UDP port range used by Asterisk RTP.

3. Source restriction
- Provider/global CIDRs.
- Trunk-specific CIDRs where policy requires network-level allow.

4. Operational safety
- Priority management to avoid accidental broad allow.
- Deterministic rule names for idempotent upsert/delete.

## 8. Data model additions required now

1. `provisioning_jobs`
- `id`, `scope_type`, `scope_id`, `trigger`, `requested_by`, `status`, `attempt_count`, `next_retry_at`, timestamps.

2. `provisioning_job_steps`
- `job_id`, `step`, `status`, `idempotency_key`, `external_ref`, `error_code`, `error_message`, timestamps.

3. `policy_sync_status`
- `scope_type`, `scope_id`, `status`, `last_success_at`, `last_attempt_at`, `last_error`, `last_job_id`.

4. `applied_state_refs`
- Mapping from internal IDs to SBC object IDs, NSG rule IDs, and last applied revision/hash.

## 9. Ordering and idempotency rules

Per-trunk apply order:
1. trunk existence/state
2. ingress hosts
3. auth users
4. routing rules
5. ACL/CIDR
6. verification

Global apply order:
1. provider/global CIDR policy
2. NSG global rule update
3. verification

Idempotency requirements:
- Stable external keys derived from internal IDs.
- Upsert-first semantics.
- Compare desired hash vs applied hash to skip no-op changes.

## 10. API behavior requirements

Existing endpoint behavior:
1. Keep current request/response shape for UI compatibility.
2. After commit, enqueue provisioning job and return accepted state.
3. UI sees sync `pending` until apply completes.

Add now:
1. `GET /provisioning/jobs`
2. `GET /provisioning/jobs/{id}`
3. Optional `POST /provisioning/reconcile` (admin only)

## 11. Definition of done

Backend integration is complete only when:
1. Every relevant UI write triggers real SBC + NSG apply job.
2. Live calls remain stable during normal config updates.
3. Sync status reflects real apply outcome.
4. Audit records show full trace from API request to SBC/NSG result.
5. Drift is detected and auto-repaired.
6. Repeated writes are idempotent with no duplicate external objects.

## 12. Implementation order (now)

1. Job framework + status tables + endpoint enqueue wiring.
2. SBC adapter with safe apply/reload and verification.
3. NSG adapter with deterministic rule management.
4. Policy sync status endpoint backed by real job data.
5. Audit + metrics.
6. Reconciler + drift healing.

## 13. VM script contract in this repo

Ready-to-use template artifacts are now included:
1. [VM_BACKEND_PROVISIONING_SETUP.md](c:\Users\HP Victus\OneDrive\coding\agentic-sip-trunk\docs\VM_BACKEND_PROVISIONING_SETUP.md)
2. `provisioning/bin/apply_trunk.sh`
3. `provisioning/bin/verify_trunk.sh`

Use these as the baseline worker-to-VM integration contract.
