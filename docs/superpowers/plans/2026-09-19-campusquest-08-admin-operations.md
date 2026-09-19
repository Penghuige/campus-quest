# CampusQuest 08 Admin, Audit & Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement immutable audit records, Admin/Teacher operational APIs, whitelist imports, system configuration, reward/catalog administration, anonymous identity reveal auditing, and safe administrative state repair.

**Architecture:** Admin routes are orchestration surfaces over existing domain services. They must not bypass Task, Submission, Points, Community, or Notification services. AuditLog records sensitive actions with redacted snapshots and request IDs.

**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL, pytest.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`

**Required quality references:** `AGENTS.md`, `docs/quality/backend-engineering.md`, `docs/quality/quality-gates.md`, `docs/quality/agent-tooling.md`.

## Global Constraints

- Sensitive admin actions write AuditLog.
- AuditLog is not deletable from normal admin UI/API.
- Snapshots must exclude password hashes, OTPs, refresh tokens, TOTP secrets, and raw recovery codes.
- Teacher resource ownership still applies in teacher console.
- Admin state repair requires reason and must preserve historical records.
- Bulk imports use preview then confirm.
- Anonymous identity reveal is explicit, permissioned, reasoned, and audited.

## Review Focus

1. Audit snapshots must redact secrets even when nested in dict/list structures.
2. A Teacher must not gain global permissions by calling an Admin URL directly.
3. Whitelist preview/confirm races must resolve to deterministic conflicts, not partial silent imports.
4. Admin points adjustment must not alter ranking unless an explicit supported ranking-affecting operation is chosen.
5. Forced state repair must not delete/rewrite immutable Claim/Submission/Ledger history.

---

### Task 1: Add AuditLog Model and Redaction

**Files:**
- Create: `backend/app/modules/audit/models.py`
- Create: `backend/app/modules/audit/service.py`
- Create: `backend/alembic/versions/0009_audit.py`
- Create: `backend/tests/unit/audit/test_redaction.py`
- Create: `backend/tests/integration/audit/test_audit_log.py`

**Interfaces:**
- Produces `AuditService.record(actor, action, resource, before, after, reason, request_id)`.

- [ ] **Step 1: Write recursive redaction test**

Input snapshot contains `password_hash`, `otp`, `refresh_token`, `totp_secret`, and nested recovery codes. Assert stored snapshot replaces values with `"[REDACTED]"`.

- [ ] **Step 2: Write append-only test**

Normal repository exposes insert/query only; no delete/update API. Database role used by app should not need routine DELETE on audit table.

- [ ] **Step 3: Implement and migrate**

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/audit backend/alembic/versions/0009_audit.py backend/tests
git commit -m "feat: record immutable redacted audit events"
```

### Task 2: Integrate Existing Audit Ports

**Files:**
- Modify: `backend/app/modules/identity/staff_service.py`
- Modify: `backend/app/modules/submissions/review_service.py`
- Modify: `backend/app/modules/points/ledger_service.py`
- Modify: `backend/app/modules/community/moderation_service.py`
- Create: `backend/tests/integration/audit/test_sensitive_actions.py`

**Interfaces:**
- Replaces fake audit ports with `AuditService`.

- [ ] **Step 1: Write action coverage test**

Assert audit rows exist for:
- staff role creation/promotion;
- malicious reward-lock invalidation;
- admin points adjustment/reversal;
- comment moderation delete;
- anonymous identity reveal.

- [ ] **Step 2: Implement transactional audit where required**

For state mutation, audit insert belongs in same transaction where practical. Identity reveal is a read action but still records audit before returning identity.

- [ ] **Step 3: Run and commit**

```bash
git add backend/app/modules backend/tests/integration/audit
git commit -m "feat: audit sensitive CampusQuest operations"
```

### Task 3: Implement StudentWhitelist Bulk Administration

**Files:**
- Create: `backend/app/modules/identity/whitelist_admin.py`
- Create: `backend/tests/integration/identity/test_whitelist_admin.py`

**Interfaces:**
- Produces `preview_whitelist_import`, `confirm_whitelist_import`, enable/disable operations.

- [ ] **Step 1: Write preview tests**

Rows include valid number, full-width digits, duplicate in file, existing DB entry, invalid length. Return row numbers/codes.

- [ ] **Step 2: Write concurrent confirm test**

Two Admins confirm overlapping imports. Unique DB constraint yields deterministic duplicate result; no partial unreported overwrite.

- [ ] **Step 3: Implement audit**

Import summary and enable/disable changes produce AuditLog entries.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/identity/whitelist_admin.py backend/tests/integration/identity/test_whitelist_admin.py
git commit -m "feat: administer student whitelist imports"
```

### Task 4: Implement RewardItem Administration and Authorized Redemption Review

**Files:**
- Create: `backend/app/modules/points/admin_service.py`
- Create: `backend/tests/integration/points/test_reward_admin.py`

**Interfaces:**
- Produces reward create/update/disable, Teacher reviewer authorization, Admin redemption queue.

- [ ] **Step 1: Write permissions tests**

Teacher without explicit reward-review grant cannot approve. Granted Teacher can review only configured course/task scope. Admin can globally review.

- [ ] **Step 2: Implement update rules**

Changing point_cost/limits affects future redemption requests; existing active reservation keeps snapshotted cost.

- [ ] **Step 3: Run and commit**

```bash
git add backend/app/modules/points/admin_service.py backend/tests/integration/points/test_reward_admin.py
git commit -m "feat: administer reward catalog and reviewers"
```

### Task 5: Implement System Configuration Store

**Files:**
- Create: `backend/app/modules/audit/system_settings.py`
- Create: `backend/alembic/versions/0010_system_settings.py`
- Create: `backend/tests/integration/audit/test_system_settings.py`

**Interfaces:**
- Produces typed settings for emoji whitelist, abandon daily limit, optional policy toggles; deployment secrets remain environment variables and never enter this store.

- [ ] **Step 1: Write type/permission tests**

Student/Teacher cannot mutate global settings. Invalid emoji-list type rejected. Admin change audited. Add NotificationTemplate CRUD tests: Admin can change channel template text/version; Teacher cannot modify global templates; unsafe executable template expressions are rejected.

- [ ] **Step 2: Implement versioned values and notification-template administration**

Runtime system values include emoji whitelist, abandon daily limit, `CURRENT_ACADEMIC_TERM` (non-empty stable key such as `2026-fall`), and management-network policy toggles/CIDRs. Changing `CURRENT_ACADEMIC_TERM` affects only future RewardRedemption snapshots and must be audited. NotificationTemplate remains its dedicated typed model from Plan 07, but its Admin create/update/enable operations are implemented in this task and emit AuditLog.

Each change increments version and records old/new redacted value.

- [ ] **Step 3: Run and commit**

```bash
git add backend/app/modules/audit/system_settings.py backend/alembic/versions/0010_system_settings.py backend/tests/integration/audit/test_system_settings.py
git commit -m "feat: add audited runtime system settings"
```

### Task 6: Implement Safe Admin Points Adjustment

**Files:**
- Modify: `backend/app/modules/points/admin_service.py`
- Create: `backend/tests/integration/points/test_admin_adjustment.py`

**Interfaces:**
- Produces `admin_adjust_points(actor, user_id, amount, reason) -> PointsLedger`.

- [ ] **Step 1: Write ranking-isolation test**

Add +500 ADMIN_ADJUSTMENT. Wallet increases by 500; daily/monthly/all ranking scores unchanged.

- [ ] **Step 2: Require non-empty reason and Admin role**

- [ ] **Step 3: Implement via LedgerService, never direct wallet update**

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/points/admin_service.py backend/tests/integration/points/test_admin_adjustment.py
git commit -m "feat: adjust user points with audited ledger entries"
```

### Task 7: Implement Safe State Repair Commands

**Files:**
- Create: `backend/app/modules/audit/repair_service.py`
- Create: `backend/tests/integration/audit/test_repairs.py`

**Interfaces:**
- Produces narrowly scoped repair operations, not a generic arbitrary SQL endpoint.

- [ ] **Step 1: Write allowed repair test**

Example: Admin may release an OCCUPIED Assignment only when its Claim is terminal/inconsistent; operation records reason and before/after.

- [ ] **Step 2: Write forbidden destructive repair tests**

Reject deleting completed Claim history, rewriting existing Ledger amount, deleting Submission review history.

- [ ] **Step 3: Implement named commands**

No endpoint accepts table/column/raw SQL from client.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/audit/repair_service.py backend/tests/integration/audit/test_repairs.py
git commit -m "feat: add constrained audited state repairs"
```

### Task 8: Implement User Account Status Administration and Backend Access Policy

**Files:**
- Create: `backend/app/modules/identity/account_admin_service.py`
- Create: `backend/app/core/admin_network_policy.py`
- Create: `backend/tests/integration/admin/test_account_status.py`
- Create: `backend/tests/unit/admin/test_network_policy.py`

**Interfaces:**
- Produces `suspend_user`, `ban_user`, `reactivate_user`; optional configured Admin/Teacher CIDR/VPN allowlist check.

- [ ] **Step 1: Write status-transition tests**

Admin can ACTIVE->SUSPENDED, ACTIVE->BANNED, SUSPENDED->ACTIVE, and explicitly unban BANNED->ACTIVE with reason. Student/Teacher without Admin authority cannot perform these transitions. Existing ledger/audit/claim history remains intact.

- [ ] **Step 2: Write enforcement test**

After suspension, an already-issued Student access token cannot claim, upload, or create community content because service/dependency re-checks account status.

- [ ] **Step 3: Implement audited transitions**

Every status change stores reason, actor, before/after snapshot, and request id.

- [ ] **Step 4: Implement optional management-network restriction**

Parse configured CIDR allowlist using standard IP network parsing. When enabled, Teacher/Admin management routes reject requests outside allowed networks after normal authentication; when disabled, normal 2FA/RBAC still applies. Never trust an arbitrary client-supplied `X-Forwarded-For` unless the deployment's trusted-proxy configuration explicitly enables it.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/app/modules/identity/account_admin_service.py backend/app/core/admin_network_policy.py backend/tests
git commit -m "feat: administer account status and management network policy"
```

### Task 9: Add Teacher/Admin Operational APIs

**Files:**
- Create: `backend/app/modules/audit/router.py`
- Create: `backend/app/modules/identity/admin_router.py`
- Create: `backend/app/modules/points/admin_router.py`
- Create: `backend/tests/integration/admin/test_admin_api.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces paginated admin/teacher endpoints for whitelist, users, reward catalog, redemption queue, audit search, failed notifications, system settings, identity reveal, and named repair commands.

- [ ] **Step 1: Write direct-URL privilege tests**

Authenticated Teacher calls Admin whitelist/settings/points-adjust endpoints -> 403. Student -> 403. Admin -> allowed.

- [ ] **Step 2: Implement pagination and filtering**

Audit and operations lists must not expose secrets or unlimited rows.

- [ ] **Step 3: Run module gate**

```bash
cd backend
pytest tests/unit/audit tests/integration/audit tests/integration/admin tests/integration/identity/test_whitelist_admin.py tests/integration/points/test_admin_adjustment.py -v
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/modules backend/app/main.py backend/tests
git commit -m "feat: expose audited teacher and admin operations"
```
