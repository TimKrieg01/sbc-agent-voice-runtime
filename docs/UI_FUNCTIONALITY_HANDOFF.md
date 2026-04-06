# UI Functionality Handoff (Current State)

This document describes the current user-interface functionality and backend contract that the SBC team should treat as the control-plane source of truth.

## 1. What the UI currently manages

The UI is a control panel for configuration only (no SIP signaling/media logic in UI).

Current managed areas:
- Organizations (tenants)
- Inbound trunks per organization
- Ingress hosts per trunk
- Optional auth users per trunk
- Routing rules per trunk
- Source CIDR allowlists (tenant + provider/global)
- Audit/event history
- Policy sync status visibility
- Admin panel for user/membership management

## 2. Tenancy model

- `Organization` is the tenant boundary.
- Tenant access is defined in `organization_memberships`:
  - `user_subject` (Entra-derived subject, normalized lowercase)
  - `org_id`
  - tenant `role` (`viewer|operator|admin`)
  - `is_active`

Authorization behavior:
- Org-scoped read requires tenant role `viewer+`.
- Org-scoped write requires tenant role `operator+`.
- Platform `admin` (Entra app role) can access across tenants.

## 3. Auth and role model

Two layers are used:

1. Platform role (from Entra token via `/me`):
- `viewer`: app access baseline
- `admin`: can use global admin APIs/panel
- `operator`: treated as legacy at platform level

2. Tenant role (from `/me/memberships`):
- Drives org-level permissions in UI (read vs write).

Frontend expectation:
- UI visibility for org actions must be based on selected-org tenant role.
- Admin panel visibility must be based on platform role `admin`.

## 4. User management model

`app_users` is a DB-level app directory (not Entra provisioning):
- stores `subject`, display metadata (`display_name`, `email`), and `is_active`.
- used by admin panel to register/manage users for app-internal assignment workflows.

`organization_memberships` links app users to orgs with tenant roles.

## 5. UI pages and actions (current)

### A. Organizations
- List organizations available to current user.
- Create organization (platform admin only).
- Edit organization metadata / active state (tenant operator+).

### B. Trunks
- List trunks for selected organization.
- Create trunk (tenant operator+).
- Edit trunk.
- Enable/disable trunk.

### C. Ingress Hosts
- Add/list/delete ingress hosts for a trunk.
- Host uniqueness enforced.

### D. Trunk Auth Users (optional hardening)
- Add/list/delete auth users per trunk.

### E. Routing Rules
- Add/list/edit/delete routing rules per trunk.
- `backend_url` must be HTTPS.

### F. Security
- Add/list/delete trunk source CIDRs.
- Provider/global CIDRs: admin-only.
- Effective source CIDR view: admin-only.
- Policy sync status: admin-only.

### G. Audit
- Audit event stream: admin-only.

### H. Admin Panel (platform admin only)
- List/create/update `app_users`.
- List/create/update `organization_memberships`.

## 6. API endpoints used by UI

Identity and membership:
- `GET /me`
- `GET /me/memberships`

Organizations:
- `POST /orgs` (admin only)
- `GET /orgs`
- `GET /orgs/{orgId}`
- `PATCH /orgs/{orgId}`

Trunks:
- `POST /orgs/{orgId}/trunks`
- `GET /orgs/{orgId}/trunks`
- `GET /trunks/{trunkId}`
- `PATCH /trunks/{trunkId}`
- `POST /trunks/{trunkId}/disable`
- `POST /trunks/{trunkId}/enable`

Ingress hosts:
- `POST /trunks/{trunkId}/ingress-hosts`
- `GET /trunks/{trunkId}/ingress-hosts`
- `DELETE /trunks/{trunkId}/ingress-hosts/{host}`

Trunk auth users:
- `POST /trunks/{trunkId}/auth-users`
- `GET /trunks/{trunkId}/auth-users`
- `DELETE /trunks/{trunkId}/auth-users/{authUser}`

Routing:
- `POST /trunks/{trunkId}/routing-rules`
- `GET /trunks/{trunkId}/routing-rules`
- `PATCH /routing-rules/{ruleId}`
- `DELETE /routing-rules/{ruleId}`

CIDRs and security:
- `POST /trunks/{trunkId}/source-cidrs`
- `GET /trunks/{trunkId}/source-cidrs`
- `DELETE /trunks/{trunkId}/source-cidrs/{cidr}`
- `POST /security/provider-cidrs` (admin only)
- `GET /security/provider-cidrs` (admin only)
- `DELETE /security/provider-cidrs/{cidr}` (admin only)
- `GET /security/effective-source-cidrs` (admin only)
- `GET /security/policy-sync-status` (admin only)

Audit:
- `GET /audit/events` (admin only)

Admin panel:
- `GET /admin/users`
- `POST /admin/users`
- `PATCH /admin/users/{userId}`
- `GET /admin/organization-memberships`
- `POST /admin/organization-memberships`
- `PATCH /admin/organization-memberships/{membershipId}`

## 7. Data model relevant for SBC collaboration

Primary control-plane entities:
- `organizations`
- `inbound_trunks`
- `trunk_ingress_hosts`
- `trunk_auth_users`
- `routing_rules`
- `provider_global_source_cidrs`
- `trunk_source_cidrs`
- `audit_events`
- `app_users`
- `organization_memberships`

## 8. What the SBC team should provide back

To implement real SIP trunk provisioning automation, please provide:
- Exact SBC/provider objects that each UI entity maps to.
- Required provisioning operations (create/update/disable/delete) per object.
- API/automation method (REST, CLI, Terraform, etc.).
- Required ordering/transaction constraints across operations.
- Failure and rollback expectations.
- Idempotency expectations.
- Which UI changes should trigger immediate SBC config apply vs queued job.

