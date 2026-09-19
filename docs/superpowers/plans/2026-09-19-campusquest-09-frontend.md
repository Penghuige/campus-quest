# CampusQuest 09 Frontend Product Flows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the responsive Next.js/PWA user, Teacher, and Admin interfaces for all CampusQuest V1 flows without duplicating backend business logic.

**Architecture:** Frontend feature folders mirror backend domains. A single typed API client consumes generated/OpenAPI-derived request and response types. Server responses are authoritative for reward tiers, deadlines, points, rankings, permissions, and validation reports; frontend countdowns and progress indicators are presentation only.

**Tech Stack:** Next.js, TypeScript, React, Playwright, a lightweight component library selected during implementation, OpenAPI type generation.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`

## Global Constraints

- Responsive Web + PWA only.
- Public/student UI never displays student number, phone, email, object key, or anonymous identity.
- Rankings display nickname + honor + score/rank only.
- Student cannot select Assignment.
- Frontend does not compute authoritative reward tier or mutate points.
- Teacher/Admin routes are protected in UI but backend remains authoritative.
- Anonymous comment display never leaks hidden identity in DOM attributes, serialized props, or accessibility labels.
- All times render from backend UTC instants using configured business/user display timezone.

## Review Focus

1. Sensitive fields must not accidentally render or remain in page source/DOM for anonymous/public views.
2. Countdown crossing a deadline must refresh authoritative backend state rather than locally award/deduct points.
3. Upload page must handle validation failure, retry, revision-required, and late windows without losing prior reports.
4. Mobile layouts must keep claim/submit/review actions usable on narrow screens.
5. Teacher/Admin navigation must not imply permission merely because a menu item is visible; API 403 handling must be graceful.

---

### Task 1: Add Typed API Client, Session Bootstrap, and Error Mapping

**Files:**
- Create: `frontend/src/lib/api.ts`
- Create: `frontend/src/lib/errors.ts`
- Create: `frontend/src/lib/time.ts`
- Create: `frontend/src/features/auth/session.ts`
- Create: `frontend/src/__tests__/api-errors.test.ts`

**Interfaces:**
- Produces `apiRequest<T>()`, typed `ApiError`, session query, time-format helpers.

- [ ] **Step 1: Write API error mapping test**

Given backend envelope with `ASSIGNMENT_LIMIT_REACHED`, assert `ApiError.code` and details are preserved without parsing Chinese message.

- [ ] **Step 2: Implement credentials-safe API client**

Use same-origin secure cookies; never persist refresh token in localStorage.

- [ ] **Step 3: Generate/import OpenAPI types**

Add `npm run api:types` that generates types from backend OpenAPI artifact. Commit generated types if project policy chooses deterministic checked-in generation.

- [ ] **Step 4: Run frontend unit/type checks and commit**

```bash
cd frontend
npm run typecheck
npm test -- api-errors
git add .
git commit -m "feat: add typed CampusQuest frontend API client"
```

### Task 2: Implement Student Registration and Login Pages

**Files:**
- Create: `frontend/src/app/(auth)/register/page.tsx`
- Create: `frontend/src/app/(auth)/login/page.tsx`
- Create: `frontend/src/features/auth/RegisterForm.tsx`
- Create: `frontend/src/features/auth/LoginForm.tsx`
- Create: `frontend/e2e/auth.spec.ts`

**Interfaces:**
- Consumes identity API.
- Produces registration OTP flow and username/password login UX.

- [ ] **Step 1: Write Playwright registration flow**

Seed whitelist fixture, enter student number, nickname, phone, request fake OTP, verify, register, and land on student home.

- [ ] **Step 2: Add client-side convenience validation only**

Student number UI may reject obvious non-ASCII digits; backend remains authority. Nickname counter uses grapheme segmentation and shows `16/16`.

- [ ] **Step 3: Implement error states**

Render stable messages for not-whitelisted, phone already bound, OTP expired, wrong password, account not active.

- [ ] **Step 4: Run and commit**

```bash
cd frontend
npm run typecheck
npm run test:e2e -- auth.spec.ts
git add src e2e
git commit -m "feat: add student registration and login UI"
```

### Task 3: Implement Student Dashboard and Task Discovery

**Files:**
- Create: `frontend/src/app/(student)/page.tsx`
- Create: `frontend/src/app/(student)/tasks/page.tsx`
- Create: `frontend/src/app/(student)/tasks/[taskId]/page.tsx`
- Create: `frontend/src/features/tasks/TaskCard.tsx`
- Create: `frontend/src/features/tasks/ClaimButton.tsx`
- Create: `frontend/e2e/task-claim.spec.ts`

**Interfaces:**
- Consumes task/claim APIs.
- Produces current points, reward progress, active/revision claims, rank snapshot, task cards.

- [ ] **Step 1: Write claim E2E test**

Open Task -> click claim -> verify assigned platform/keyword appears only after server allocation. Ensure there is no selectable Assignment list or assignment_id input.

- [ ] **Step 2: Implement Task cards**

Show title, rarity, base reward, DDL mode, availability count, rating. For FIXED near cutoff render server eligibility status.

- [ ] **Step 3: Implement claim conflict UX**

Handle `NO_ASSIGNMENT_AVAILABLE`, `ASSIGNMENT_LIMIT_REACHED`, `TASK_ACTIVE_CLAIM_EXISTS`, `CLAIM_CUTOFF_REACHED` without generic crash.

- [ ] **Step 4: Run mobile Playwright viewport**

Use 375x812 and desktop viewport; claim CTA must remain visible and usable.

- [ ] **Step 5: Commit**

```bash
git add frontend/src frontend/e2e/task-claim.spec.ts
git commit -m "feat: add student task discovery and claiming UI"
```

### Task 4: Implement Claim Detail, Upload, Validation, and Revision UX

**Files:**
- Create: `frontend/src/app/(student)/claims/[claimId]/page.tsx`
- Create: `frontend/src/features/submissions/UploadPanel.tsx`
- Create: `frontend/src/features/submissions/ValidationReport.tsx`
- Create: `frontend/src/features/submissions/RewardStatus.tsx`
- Create: `frontend/e2e/submission.spec.ts`

**Interfaces:**
- Consumes upload-intent/finalize/validation APIs and Claim view.
- Produces direct-to-object-storage upload flow and validation/revision feedback.

- [ ] **Step 1: Write valid upload E2E**

Select CSV -> request upload intent -> upload via test storage -> finalize -> poll validation -> display report -> show UNDER_REVIEW.

- [ ] **Step 2: Write validation-failure E2E**

Missing required field shows structured errors and retry action.

- [ ] **Step 3: Implement authoritative reward display**

Render backend `current_reward_points` / lock status. Never calculate 80/50/20 in frontend.

- [ ] **Step 4: Implement revision state**

Show Teacher note, retained reward lock, revision deadline, previous versions, and upload new version.

- [ ] **Step 5: Test deadline crossing**

Freeze browser/backend test clock where supported. When local countdown reaches zero, refetch Claim; do not locally set EXPIRED.

- [ ] **Step 6: Commit**

```bash
git add frontend/src frontend/e2e/submission.spec.ts
git commit -m "feat: add submission validation and revision UI"
```

### Task 5: Implement Points, Rewards, Rankings, Honors, and Growth Pages

**Files:**
- Create: `frontend/src/app/(student)/rewards/page.tsx`
- Create: `frontend/src/app/(student)/rankings/page.tsx`
- Create: `frontend/src/app/(student)/profile/page.tsx`
- Create: `frontend/src/features/rewards/RedeemDialog.tsx`
- Create: `frontend/src/features/rankings/Leaderboard.tsx`
- Create: `frontend/e2e/rewards-ranking.spec.ts`

**Interfaces:**
- Consumes points/reward/ranking/growth APIs.

- [ ] **Step 1: Write redemption E2E**

Show available points -> request reward -> available display reflects frozen spendability -> rejection restores -> approval displays pending fulfillment/fulfilled state.

- [ ] **Step 2: Write ranking privacy test**

Rendered ranking rows contain nickname/honor/score only. Assert seeded student numbers/phones/emails do not appear in page content.

- [ ] **Step 3: Implement tabs**

Daily/monthly/all and around-me. Around-me visually marks current user.

- [ ] **Step 4: Implement growth profile**

Month points/rank, total contribution, completion count, on-time rate, streak, best month, honor selector.

- [ ] **Step 5: Run and commit**

```bash
git add frontend/src frontend/e2e/rewards-ranking.spec.ts
git commit -m "feat: add points rewards rankings and growth UI"
```

### Task 6: Implement Community UI

**Files:**
- Create: `frontend/src/features/community/CommentComposer.tsx`
- Create: `frontend/src/features/community/CommentThread.tsx`
- Create: `frontend/src/features/community/Reactions.tsx`
- Create: `frontend/src/features/community/TaskRating.tsx`
- Modify: `frontend/src/app/(student)/tasks/[taskId]/page.tsx`
- Create: `frontend/e2e/community.spec.ts`

**Interfaces:**
- Consumes Community APIs.

- [ ] **Step 1: Write anonymous DOM privacy test**

Post anonymous comment as seeded Student. As another Student, assert page text and DOM do not contain original nickname, student number, phone, email, or user id.

- [ ] **Step 2: Implement composer**

Explicit “公开昵称 / 匿名” choice, default defined by product implementation; display clear preview of chosen identity mode.

- [ ] **Step 3: Implement two-level visual threading**

Deep replies remain in root thread without increasing indentation indefinitely.

- [ ] **Step 4: Implement vote/reaction/report/rating states**

Optimistic UI is allowed only with rollback on API failure. Rating control appears enabled only when backend marks user eligible.

- [ ] **Step 5: Run and commit**

```bash
git add frontend/src frontend/e2e/community.spec.ts
git commit -m "feat: add task community and rating UI"
```

### Task 7: Implement Notification Inbox and PWA Shell

**Files:**
- Create: `frontend/src/app/(student)/notifications/page.tsx`
- Create: `frontend/src/features/notifications/NotificationBell.tsx`
- Create: `frontend/src/app/manifest.ts`
- Create: `frontend/e2e/notifications.spec.ts`

**Interfaces:**
- Consumes in-app notification API.

- [ ] **Step 1: Write inbox test**

Revision-required and reward-result notifications render and can be marked read by owner only.

- [ ] **Step 2: Add PWA manifest**

Provide app name, short name, start URL, display mode, icons supplied by project assets. Do not add push notifications in V1.

- [ ] **Step 3: Run and commit**

```bash
git add frontend/src frontend/e2e/notifications.spec.ts
git commit -m "feat: add notification inbox and PWA shell"
```

### Task 8: Implement Teacher Workspace

**Files:**
- Create: `frontend/src/app/teacher/layout.tsx`
- Create: `frontend/src/app/teacher/tasks/page.tsx`
- Create: `frontend/src/app/teacher/tasks/[taskId]/page.tsx`
- Create: `frontend/src/app/teacher/reviews/page.tsx`
- Create: `frontend/src/features/admin/AssignmentImport.tsx`
- Create: `frontend/src/features/admin/SubmissionReview.tsx`
- Create: `frontend/e2e/teacher.spec.ts`

**Interfaces:**
- Consumes Teacher Task/import/review/community APIs.

- [ ] **Step 1: Write assignment import preview test**

Upload file -> display total/valid/error rows -> require confirm -> result summary.

- [ ] **Step 2: Write review test**

Teacher sees validation report/sample/history, can request revision with note or approve. Another Teacher's unshared Task returns access-denied UX.

- [ ] **Step 3: Implement community moderation without anonymous identity leakage**

Teacher moderation card for anonymous comment must not contain student number/phone/email/login identifier.

- [ ] **Step 4: Run and commit**

```bash
git add frontend/src frontend/e2e/teacher.spec.ts
git commit -m "feat: add teacher task and review workspace"
```

### Task 9: Implement Admin Workspace

**Files:**
- Create: `frontend/src/app/admin/layout.tsx`
- Create: `frontend/src/app/admin/users/page.tsx`
- Create: `frontend/src/app/admin/whitelist/page.tsx`
- Create: `frontend/src/app/admin/rewards/page.tsx`
- Create: `frontend/src/app/admin/redemptions/page.tsx`
- Create: `frontend/src/app/admin/audit/page.tsx`
- Create: `frontend/src/app/admin/system/page.tsx`
- Create: `frontend/e2e/admin.spec.ts`

**Interfaces:**
- Consumes Admin APIs.

- [ ] **Step 1: Write privilege navigation/API-failure test**

Teacher manually opens `/admin/whitelist`; UI redirects/403 page, and no Admin data is rendered.

- [ ] **Step 2: Implement explicit anonymous reveal dialog**

Require Admin to enter reason; only after successful reveal API display identity. Do not auto-fetch identities when loading comments.

- [ ] **Step 3: Implement audited destructive-action confirmations**

Suspend/ban, reward adjustment, system setting update, named state repair show reason/confirmation as required by backend.

- [ ] **Step 4: Run and commit**

```bash
git add frontend/src frontend/e2e/admin.spec.ts
git commit -m "feat: add CampusQuest admin workspace"
```

### Task 10: Frontend Verification Gate

**Files:**
- Modify: `frontend/package.json`
- Modify: `Makefile`

**Interfaces:**
- Produces deterministic `npm run verify` and integration with root `make verify`.

- [ ] **Step 1: Add scripts**

At minimum:
- `typecheck`
- `lint`
- unit test
- `test:e2e`
- `build`

- [ ] **Step 2: Run complete frontend gate**

```bash
cd frontend
npm run typecheck
npm run lint
npm test
npm run build
npm run test:e2e
```

Expected: all exit 0.

- [ ] **Step 3: Commit**

```bash
git add frontend/package.json Makefile
git commit -m "chore: add CampusQuest frontend verification gate"
```
