# CampusQuest 06 Community & Ratings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement task comments, per-comment anonymity, threaded replies, soft delete/edit history, like/dislike voting, emoji reactions, reports, task ratings, and moderation permission boundaries.

**Architecture:** Community records always retain real `user_id` internally. Public serializers decide whether identity is visible; Admin identity reveal is a separate audited operation rather than an automatic field on normal moderation queries.

**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL, pytest.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`

## Global Constraints

- Comments publish immediately without pre-moderation.
- Anonymous is per comment, not per account.
- Ordinary users cannot identify anonymous authors.
- Teacher moderation views do not expose student number, phone, email, or login username for anonymous comments.
- Admin reveal requires permission, reason, and audit event.
- Soft delete preserves child replies.
- Comment length default maximum 2000 characters.
- One vote per user/comment; one same-emoji reaction per user/comment.
- Only users with COMPLETED Claim on a Task can rate it.
- One rating per user/task, editable.

## Review Focus

1. Anonymous serialization must not accidentally leak user identifiers in nested objects or error payloads.
2. Reply parent must belong to the same Task; cross-task references and cycles must be rejected.
3. Concurrent vote/reaction toggles must preserve one-row uniqueness and correct final state.
4. Deleting a parent comment must keep children reachable.
5. XSS payloads must be stored/rendered as plain text rather than executable HTML.

---

### Task 1: Add Community Models and Constraints

**Files:**
- Create: `backend/app/modules/community/models.py`
- Create: `backend/app/modules/community/enums.py`
- Create: `backend/alembic/versions/0007_community.py`
- Create: `backend/tests/integration/community/test_community_constraints.py`

**Interfaces:**
- Produces `Comment`, `CommentRevision`, `CommentVote`, `CommentReaction`, `CommentReport`, `TaskRating`.

- [ ] **Step 1: Write DB constraint tests**

Assert unique vote, unique same emoji reaction, unique task rating, rating check 1–5, and report uniqueness policy.

- [ ] **Step 2: Implement models/migration**

Use soft-delete columns. Comment content remains text, not HTML.

- [ ] **Step 3: Run and commit**

```bash
git add backend/app/modules/community backend/alembic/versions/0007_community.py backend/tests/integration/community
git commit -m "feat: add community persistence"
```

### Task 2: Implement Comment Creation and Safe Serialization

**Files:**
- Create: `backend/app/modules/community/schemas.py`
- Create: `backend/app/modules/community/comment_service.py`
- Create: `backend/app/modules/community/serializers.py`
- Create: `backend/tests/unit/community/test_comment_serialization.py`
- Create: `backend/tests/integration/community/test_comments.py`

**Interfaces:**
- Produces `create_comment`, `list_comments`, public/moderation serializers.

- [ ] **Step 1: Write anonymous leakage test**

Create anonymous comment by Student with known student number/phone/email. Serialize as ordinary Student and assert none of those values or raw `user_id` appear.

- [ ] **Step 2: Write content tests**

Reject whitespace-only and >2000-character content. Store `<img src=x onerror=alert(1)>` literally; API returns it as JSON text.

- [ ] **Step 3: Implement parent validation**

Parent must exist, be in same Task, and cannot create a cycle. Since create-only parent pointer cannot point to the new row itself, cross-task validation is the primary V1 cycle guard; edit APIs must never allow changing `parent_id`.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/community backend/tests/unit/community backend/tests/integration/community
git commit -m "feat: publish safe public and anonymous comments"
```

### Task 3: Implement Edit History and Soft Delete

**Files:**
- Modify: `backend/app/modules/community/comment_service.py`
- Create: `backend/tests/integration/community/test_comment_edit_delete.py`

**Interfaces:**
- Produces `edit_comment`, `delete_own_comment`, `moderate_delete_comment`.

- [ ] **Step 1: Write ownership tests**

Owner can edit/delete; another Student cannot. Edit creates CommentRevision containing previous content.

- [ ] **Step 2: Write parent-delete test**

Delete parent with child reply. Public thread contains tombstone parent “该评论已删除” and child remains.

- [ ] **Step 3: Implement Teacher moderation boundary**

Teacher may moderate only owned/collaborating Task and must provide delete reason for moderation delete.

- [ ] **Step 4: Add Admin hard-hide subtree regression**

For a privacy/legal removal case, Admin may hard-hide the visible content of a comment subtree through a named moderation command with a required reason and audit event. Historical IDs/relations and AuditLog remain; this is not a database cascade delete.

- [ ] **Step 5: Run and commit**

```bash
git add backend/app/modules/community/comment_service.py backend/tests/integration/community/test_comment_edit_delete.py
git commit -m "feat: edit and soft delete comments with history"
```

### Task 4: Implement Like/Dislike Vote Toggle

**Files:**
- Create: `backend/app/modules/community/vote_service.py`
- Create: `backend/tests/integration/community/test_votes.py`

**Interfaces:**
- Produces `set_vote(user_id, comment_id, value: Literal[-1,0,1])`.

- [ ] **Step 1: Write transition tests**

none -> +1; +1 -> 0; +1 -> -1; -1 -> +1. Assert one row max.

- [ ] **Step 2: Write concurrent same-user vote test**

Two concurrent requests switching the same vote must not violate unique constraints or create two rows. Final value must equal one committed transition and counts must match rows.

- [ ] **Step 3: Implement row lock/upsert**

Lock existing vote row where present; rely on unique constraint and retry conflict if both create from none.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/community/vote_service.py backend/tests/integration/community/test_votes.py
git commit -m "feat: toggle comment votes atomically"
```

### Task 5: Implement Emoji Reactions

**Files:**
- Create: `backend/app/modules/community/reaction_service.py`
- Create: `backend/tests/integration/community/test_reactions.py`

**Interfaces:**
- Produces `toggle_reaction(user_id, comment_id, emoji) -> bool`.

- [ ] **Step 1: Write whitelist tests**

Default allowed set contains 👍 ❤️ 😂 🎉 😭 👀 🤔 🔥. Unknown emoji rejected.

- [ ] **Step 2: Write toggle/idempotency tests**

Same emoji click creates then removes. Two different emojis may coexist for one user/comment.

- [ ] **Step 3: Implement against Admin-configurable whitelist port**

Plan 08 supplies persistent setting; this plan uses default provider/fake.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/community/reaction_service.py backend/tests/integration/community/test_reactions.py
git commit -m "feat: add comment emoji reactions"
```

### Task 6: Implement Reports

**Files:**
- Create: `backend/app/modules/community/report_service.py`
- Create: `backend/tests/integration/community/test_reports.py`

**Interfaces:**
- Produces `report_comment(user_id, comment_id, category, note)`, `list_task_reports(actor, task_id)`.

- [ ] **Step 1: Write report tests**

Report does not hide/delete comment automatically. Duplicate same-user/same-comment/same-category is idempotent or rejected with stable code.

- [ ] **Step 2: Implement categories**

`SPAM`, `HARASSMENT`, `PRIVACY`, `OTHER`.

- [ ] **Step 3: Ensure reported user never receives reporter identity through public APIs**

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/community/report_service.py backend/tests/integration/community/test_reports.py
git commit -m "feat: add non-destructive comment reporting"
```

### Task 7: Implement Task Ratings

**Files:**
- Create: `backend/app/modules/community/rating_service.py`
- Create: `backend/tests/integration/community/test_ratings.py`

**Interfaces:**
- Produces `rate_task(user_id, task_id, rating) -> TaskRating`, `rating_summary(task_id)`.

- [ ] **Step 1: Write eligibility tests**

Student with only CLAIMED/ABANDONED Claim rejected. Student with at least one COMPLETED Claim succeeds.

- [ ] **Step 2: Write update test**

Rate 3 then 5; DB retains one row with 5 and summary updates.

- [ ] **Step 3: Implement aggregate query**

Return average and count; do not expose individual rating identities publicly.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/community/rating_service.py backend/tests/integration/community/test_ratings.py
git commit -m "feat: rate completed tasks"
```

### Task 8: Implement Anonymous Moderation and Admin Reveal Port

**Files:**
- Modify: `backend/app/modules/community/serializers.py`
- Create: `backend/app/modules/community/moderation_service.py`
- Create: `backend/tests/integration/community/test_anonymous_moderation.py`

**Interfaces:**
- Produces Teacher-safe moderation records and `request_identity_reveal(admin_actor, comment_id, reason)`.

- [ ] **Step 1: Write Teacher-safe serialization test**

Anonymous moderation record may contain a pseudonymous moderation key but not student number, username, phone, email, or raw public user identifier.

- [ ] **Step 2: Write Admin reveal test with fake AuditPort**

Missing reason -> rejected. Valid Admin reveal returns identity and emits audit event containing actor, comment id, reason, timestamp.

- [ ] **Step 3: Implement without auto-reveal**

Normal Admin list endpoint must remain anonymous until explicit reveal call.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/community backend/tests/integration/community/test_anonymous_moderation.py
git commit -m "feat: preserve anonymous moderation boundaries"
```

### Task 9: Add Community APIs

**Files:**
- Create: `backend/app/modules/community/router.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/integration/community/test_community_api.py`

**Interfaces:**
- Produces comment list/create/edit/delete, vote, reaction, report, rating, Teacher moderation routes.

- [ ] **Step 1: Write API flow test**

Student creates anonymous comment -> another Student sees anonymous -> votes/reacts -> owner edits -> Teacher soft-deletes -> child reply remains.

- [ ] **Step 2: Implement pagination and latest/hot sort**

Hot score is server-calculated and replaceable. Never accept client hot score.

- [ ] **Step 3: Add comment write-rate limiting**

Apply a Redis-backed/user-keyed limiter to comment create, reply, edit, vote, reaction, and report write paths with stricter limits on comment/report creation. Add an API test that repeated requests beyond the configured limit return a stable 429/business error without inserting extra rows.

- [ ] **Step 4: Run module gate**

```bash
cd backend
pytest tests/unit/community tests/integration/community -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/modules/community/router.py backend/app/main.py backend/tests
git commit -m "feat: expose CampusQuest community APIs"
```
