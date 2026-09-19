# CampusQuest 02 Identity & Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Student whitelist registration, phone verification, password/session authentication, Staff invitation and mandatory TOTP 2FA, RBAC, account status enforcement, and identity APIs.

**Architecture:** Identity owns User, StudentWhitelist, verification challenges, sessions, staff invitations, and 2FA secrets/recovery codes. Other modules consume an authenticated `Actor` and RBAC helpers; they never inspect tokens or passwords directly.

**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL, Argon2id, phonenumbers, PyOTP, signed JWT/access-token library, Redis-backed OTP challenges.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`

**Required quality references:** `AGENTS.md`, `docs/quality/backend-engineering.md`, `docs/quality/quality-gates.md`, `docs/quality/agent-tooling.md`.

## Global Constraints

- Student username is an ASCII-digit student number stored as string and must exist in StudentWhitelist.
- Phone verification is mandatory and normalized phone is globally unique.
- nickname maximum is 16 Unicode grapheme clusters.
- Student login is username + password.
- Password hashing uses Argon2id.
- Teacher/Admin accounts do not self-register through StudentWhitelist.
- Teacher/Admin must complete 2FA before management access.
- SUSPENDED/BANNED accounts cannot claim, submit, or create community content.
- Authentication secrets, OTPs, refresh tokens, and recovery codes must never be logged in plaintext.

## Review Focus

1. Unicode nickname length must count grapheme clusters rather than bytes/code points.
2. Concurrent registration of one student number or one phone must produce exactly one account.
3. OTP expiry, retry limits, resend cooldown, and replay after successful verification must be enforced.
4. Refresh-token rotation and password reset must revoke superseded sessions.
5. Staff invitation must not create a management session before mandatory TOTP setup completes.

---

### Task 1: Add Identity Models and Database Constraints

**Files:**
- Create: `backend/app/modules/identity/models.py`
- Create: `backend/app/modules/identity/enums.py`
- Create: `backend/alembic/versions/0002_identity.py`
- Create: `backend/tests/integration/identity/test_identity_constraints.py`

**Interfaces:**
- Produces `User`, `StudentWhitelist`, `UserSession`, `StaffInvitation`, `TotpCredential`, `RecoveryCode`; enums `Role` and `UserStatus`.

- [ ] **Step 1: Write constraint tests**

Create two users with the same `username` and assert the second insert raises `IntegrityError`. Repeat for normalized phone. V1 also uses a global unique constraint for a non-null normalized email address; test two accounts attempting to bind the same normalized email. Insert `"000123456"` and assert it round-trips unchanged.

- [ ] **Step 2: Run tests and verify failure**

Run: `cd backend && pytest tests/integration/identity/test_identity_constraints.py -v -m integration`.

- [ ] **Step 3: Implement models**

Use string/varchar for username and phone. Add database unique constraints on `users.username` and `users.phone_e164`, plus a partial/global unique constraint for non-null normalized `users.email_normalized`. Store password hash only. Keep `email_verified_at` nullable.

- [ ] **Step 4: Add migration and migrate from clean DB**

Run:

```bash
cd backend
alembic upgrade head
pytest tests/integration/identity/test_identity_constraints.py -v -m integration
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/identity backend/alembic/versions/0002_identity.py backend/tests/integration/identity
git commit -m "feat: add identity persistence models"
```

### Task 2: Implement Student Number and Nickname Validation

**Files:**
- Create: `backend/app/modules/identity/validation.py`
- Create: `backend/tests/unit/identity/test_validation.py`

**Interfaces:**
- Produces:
  - `validate_student_number(value: str, min_len: int = 6, max_len: int = 20) -> str`
  - `normalize_nickname(value: str) -> str`

- [ ] **Step 1: Write failing student-number parameterized tests**

```python
@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("20250010001", True),
        ("000123456", True),
        ("２０２５００１", False),
        ("2025 001", False),
        ("2025-001", False),
        ("1e10", False),
        ("", False),
    ],
)
def test_student_number_format(value, ok):
    if ok:
        assert validate_student_number(value) == value
    else:
        with pytest.raises(ValueError):
            validate_student_number(value)
```

- [ ] **Step 2: Write grapheme tests**

Use the `regex` package `\X` behavior. Test 16 Chinese characters, 17 Chinese characters, 16 family/ZWJ emoji, and a nickname that becomes empty after trimming controls/whitespace.

- [ ] **Step 3: Run and verify failure**

- [ ] **Step 4: Implement exact validators**

Student number uses `re.fullmatch(r"[0-9]+", value)`, not `str.isdigit()`, because full-width Unicode digits must fail. Nickname trims edges, removes prohibited controls, counts grapheme clusters, and rejects >16.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/app/modules/identity/validation.py backend/tests/unit/identity/test_validation.py
git commit -m "feat: validate student numbers and unicode nicknames"
```

### Task 3: Implement StudentWhitelist Queries and Registration Transaction

**Files:**
- Create: `backend/app/modules/identity/repository.py`
- Create: `backend/app/modules/identity/service.py`
- Create: `backend/app/modules/identity/schemas.py`
- Create: `backend/tests/integration/identity/test_registration.py`

**Interfaces:**
- Consumes `Clock`, password hasher, phone-normalization helper.
- Produces:
  - `IdentityService.register_student(command: RegisterStudent) -> User`
  - `StudentWhitelistRepository.require_enabled(student_number) -> StudentWhitelist`

- [ ] **Step 1: Write failing registration tests**

Cover:

- enabled whitelist + unused phone -> success.
- absent whitelist -> `STUDENT_NOT_WHITELISTED`.
- disabled whitelist -> same explicit business rejection.
- concurrent registration with same username -> one success.
- two different usernames with same phone -> one success.

The concurrent tests use two independent DB sessions and `asyncio.gather`.

- [ ] **Step 2: Run tests and verify failure**

- [ ] **Step 3: Implement registration transaction**

Registration requires a prior verified phone challenge token. Re-check whitelist and uniqueness in the transaction. Rely on DB unique constraints to close race windows and convert `IntegrityError` into stable business codes.

- [ ] **Step 4: Run tests**

Expected: no 500s in expected conflicts.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/identity backend/tests/integration/identity/test_registration.py
git commit -m "feat: register whitelisted students atomically"
```

### Task 4: Implement Phone OTP Challenge Lifecycle

**Files:**
- Create: `backend/app/modules/identity/otp.py`
- Create: `backend/tests/unit/identity/test_otp.py`
- Create: `backend/tests/integration/identity/test_otp_flow.py`

**Interfaces:**
- Produces:
  - `request_phone_challenge(raw_phone: str, purpose: OtpPurpose) -> ChallengePublic`
  - `verify_phone_challenge(challenge_id: UUID, code: str) -> VerifiedPhoneToken`

- [ ] **Step 1: Write failing tests**

Using `FrozenClock`, assert:

- TTL is 5 minutes.
- sixth wrong attempt is rejected after five failures.
- resend inside cooldown is rejected.
- successful code can be consumed once only.
- expired challenge fails.
- normalized E.164 value is stored, raw formatting variants resolve identically.

- [ ] **Step 2: Run and verify failure**

- [ ] **Step 3: Implement Redis challenge storage**

Store a cryptographic hash/HMAC of the OTP, never plaintext. Record attempts, created_at, expires_at, consumed_at, purpose, normalized phone. Use atomic Redis operations/Lua or equivalent transaction so two simultaneous verify requests cannot both consume one challenge.

- [ ] **Step 4: Integrate FakeSmsSender**

The fake receives the plaintext code only in test process memory. Production logging must not include it.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/app/modules/identity/otp.py backend/tests/unit/identity/test_otp.py backend/tests/integration/identity/test_otp_flow.py
git commit -m "feat: add replay-safe phone verification"
```

### Task 5: Implement Password Hashing and Student Login Sessions

**Files:**
- Create: `backend/app/core/security.py`
- Create: `backend/app/modules/identity/session_service.py`
- Create: `backend/tests/unit/identity/test_passwords.py`
- Create: `backend/tests/integration/identity/test_sessions.py`

**Interfaces:**
- Produces:
  - `hash_password(password: str) -> str`
  - `verify_password(password: str, encoded: str) -> bool`
  - `SessionService.login_student(username, password) -> SessionTokens`
  - `SessionService.rotate_refresh(refresh_token) -> SessionTokens`
  - `SessionService.revoke_all(user_id) -> None`

- [ ] **Step 1: Write password tests**

Assert encoded hashes do not contain plaintext, valid password verifies, wrong password fails, and overlong/underlength inputs are rejected according to configured 10–128 range.

- [ ] **Step 2: Write refresh rotation tests**

Login -> refresh A. Rotate A -> B. Reusing A must fail. Password reset -> B fails afterward.

- [ ] **Step 3: Implement Argon2id and refresh-session persistence**

Store only a hash of refresh tokens in `UserSession`. Access token contains user id, role, session id, expiry; do not trust role forever without status checks on sensitive management operations.

- [ ] **Step 4: Run tests**

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/security.py backend/app/modules/identity/session_service.py backend/tests
git commit -m "feat: add password and session authentication"
```

### Task 6: Add Staff Invitations and Mandatory TOTP 2FA

**Files:**
- Create: `backend/app/modules/identity/staff_service.py`
- Create: `backend/app/modules/identity/totp.py`
- Create: `backend/tests/integration/identity/test_staff_onboarding.py`
- Create: `backend/tests/unit/identity/test_totp.py`

**Interfaces:**
- Produces:
  - `create_staff_invitation(actor, email, role) -> StaffInvitation`
  - `accept_staff_invitation(token, password) -> PendingStaffSession`
  - `begin_totp_setup(user_id) -> TotpSetup`
  - `confirm_totp_setup(user_id, code) -> list[str]`
  - `authenticate_staff(identifier, password, totp_code) -> SessionTokens`

- [ ] **Step 1: Write staff onboarding tests**

Assert:

- Student cannot create invitation.
- Admin can invite Teacher.
- invite token expires and is single-use.
- accepting invite does not grant management session before TOTP confirmation.
- correct TOTP enables login.
- recovery code works once only.

- [ ] **Step 2: Run and verify failure**

- [ ] **Step 3: Implement invitation and TOTP**

Encrypt TOTP secret at rest using application secret-management abstraction. Store recovery codes as hashes. TOTP confirmation must prove one valid code before setting `two_factor_enabled_at`.

- [ ] **Step 4: Add role-promotion audit event interface**

Actual AuditLog persistence arrives in Plan 08; emit a typed audit/domain event now so promotion/onboarding can be consumed later.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/app/modules/identity backend/tests/integration/identity/test_staff_onboarding.py backend/tests/unit/identity/test_totp.py
git commit -m "feat: onboard staff with mandatory two factor auth"
```

### Task 7: Implement RBAC and Account-Status Dependencies

**Files:**
- Create: `backend/app/core/rbac.py`
- Create: `backend/app/modules/identity/dependencies.py`
- Create: `backend/tests/unit/identity/test_rbac.py`
- Create: `backend/tests/integration/identity/test_account_status.py`

**Interfaces:**
- Produces:
  - `get_actor() -> Actor`
  - `require_role(*roles)`
  - `require_active_actor()`
  - `Actor(user_id: UUID, role: Role)`

- [ ] **Step 1: Write RBAC tests**

Student denied Admin-only operation. Teacher allowed Teacher-or-Admin operation. SUSPENDED Student with otherwise valid token denied a state-changing student endpoint.

- [ ] **Step 2: Implement dependencies**

Resolve current User status for state-changing operations. Return `ACCOUNT_NOT_ACTIVE` for suspended/banned account rather than generic 500.

- [ ] **Step 3: Run tests**

- [ ] **Step 4: Commit**

```bash
git add backend/app/core/rbac.py backend/app/modules/identity/dependencies.py backend/tests
git commit -m "feat: enforce role and account status policies"
```

### Task 8: Implement Student Profile Contacts and Password Recovery

**Files:**
- Create: `backend/app/modules/identity/profile_service.py`
- Create: `backend/app/modules/identity/email_verification.py`
- Create: `backend/tests/integration/identity/test_profile_contacts.py`
- Create: `backend/tests/integration/identity/test_password_recovery.py`

**Interfaces:**
- Produces:
  - `change_nickname(user_id, nickname) -> User`
  - `request_phone_change(user_id, password, new_phone) -> ChallengePublic`
  - `confirm_phone_change(user_id, challenge_id, code) -> User`
  - `request_email_verification(user_id, email) -> EmailChallenge`
  - `confirm_email_verification(user_id, token) -> User`
  - `unbind_email(user_id, password) -> User`
  - `request_password_reset(username) -> PasswordResetChallenge`
  - `confirm_password_reset(challenge_id, phone_code, new_password) -> None`

- [ ] **Step 1: Write phone-change tests**

Require current password/re-authentication before challenge issuance, verify new phone OTP, preserve old phone until confirmation, reject a new phone already bound to another account, and ensure concurrent changes cannot violate the global phone unique constraint.

- [ ] **Step 2: Write email tests**

Normalize email, enforce V1 global unique verified/bound email, do not mark it verified until token consumption, make token short-lived and single-use, and allow unbind only after re-authentication. Email verification uses `EmailSender`, never logs the raw token.

- [ ] **Step 3: Write password-recovery tests**

Because every Student has a verified phone, V1 password recovery uses the bound phone as the mandatory recovery factor. After successful reset, revoke all existing refresh sessions. Replaying the same reset challenge must fail.

- [ ] **Step 4: Implement services with existing validators/challenge patterns**

Nickname update reuses `normalize_nickname`; phone change reuses E.164 normalization and OTP; staff accounts do not use the Student phone-reset path.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/app/modules/identity backend/tests/integration/identity
git commit -m "feat: manage student contacts and account recovery"
```

### Task 9: Add Identity API Routes

**Files:**
- Create: `backend/app/modules/identity/router.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/integration/identity/test_identity_api.py`

**Interfaces:**
- Produces HTTP endpoints:
  - `POST /api/v1/auth/phone/challenges`
  - `POST /api/v1/auth/phone/challenges/{id}/verify`
  - `POST /api/v1/auth/register`
  - `POST /api/v1/auth/login`
  - `POST /api/v1/auth/refresh`
  - `POST /api/v1/auth/logout`
  - password reset endpoints.
  - staff invitation/TOTP endpoints under Admin/Staff scope.
  - `PATCH /api/v1/me/nickname`
  - phone-change request/confirm endpoints
  - email bind/verify/unbind endpoints
  - password reset request/confirm endpoints.

Staff login identifier for V1 is the Staff account's verified email address. Do not support ambiguous fallback between student-number and staff-email login in one parser; Student and Staff login endpoints may be separate while sharing session infrastructure.

- [ ] **Step 1: Write API flow test**

Use TestClient/AsyncClient:

```text
seed whitelist
-> request OTP
-> verify OTP
-> register
-> login
-> GET /api/v1/me
```

Assert no response includes `password_hash`, OTP hash, refresh-token hash, or TOTP secret.

- [ ] **Step 2: Implement thin routes**

Routes validate input, call service, set secure refresh cookie, and translate domain errors. They must not contain persistence logic.

- [ ] **Step 3: Add rate-limit and CSRF hooks**

Apply request limits to login, registration, OTP send, email verification, phone change, and password reset endpoints. Test that the limiter adapter is invoked with normalized identifiers. Because refresh authentication uses secure cookies, write an integration test proving state-changing cookie-authenticated requests without the required CSRF token are rejected while the same request with a valid CSRF token succeeds.

- [ ] **Step 4: Run identity gate**

```bash
cd backend
pytest tests/unit/identity tests/integration/identity -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/identity/router.py backend/app/main.py backend/tests
git commit -m "feat: expose CampusQuest identity APIs"
```
