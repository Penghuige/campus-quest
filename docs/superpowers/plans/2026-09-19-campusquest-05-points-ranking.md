# CampusQuest 05 Points, Rewards, Rankings & Honors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement immutable points accounting, spendable balance projection, atomic reward reservations/redemptions, ranking projection/rebuild, reward reversal into original ranking periods, honors, and personal growth metrics.

**Architecture:** PointsLedger is immutable and authoritative for economic history; PointWallet/PointReservation are transactional projections for fast spendability checks. Rankings are derived from ranking-affecting ledger entries into Redis and can be rebuilt from PostgreSQL.

**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL, Redis Sorted Sets, Celery, pytest.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`

## Global Constraints

- Ledger rows are append-only.
- Assignment reward must be idempotent per Claim.
- Normal redemption changes spendable balance but not historical earned/ranking contribution.
- Admin adjustment defaults to `affects_ranking=false`.
- Reversal links original reward and corrects the original ranking period.
- Points are integer only.
- Reward percentage floor semantics are server-side.
- Redemption cannot overspend wallet or oversell inventory.
- Daily/monthly/all rankings must be rebuildable from PostgreSQL.

## Review Focus

1. Two concurrent redemptions must not spend the same points twice.
2. Two concurrent requests for the final stock item must yield at most one active reservation.
3. Duplicate submission approval must not create a second Assignment reward.
4. A September reversal of an August reward must repair August and all-time rankings without penalizing September.
5. Redis loss must be recoverable deterministically from PostgreSQL.

---

### Task 1: Add Points and Reward Models

**Files:**
- Create: `backend/app/modules/points/enums.py`
- Create: `backend/app/modules/points/models.py`
- Create: `backend/alembic/versions/0005_points_rewards.py`
- Create: `backend/tests/integration/points/test_points_constraints.py`

**Interfaces:**
- Produces `PointsLedger`, `PointWallet`, `PointReservation`, `RewardItem`, `RewardRedemption`; enums `LedgerType`, `RedemptionStatus`.

- [ ] **Step 1: Write database constraint tests**

Assert:
- one `ASSIGNMENT_REWARD` per source Claim;
- amount is integer and non-zero where required;
- active reservation references one user/redemption;
- RewardItem stock cannot be persisted negative;
- valid redemption states are constrained.

- [ ] **Step 2: Implement models and migration**

Add unique key `(source_type, source_id, ledger_type)` for idempotent original reward entries. Add `reversal_of_id` foreign key for reversals.

- [ ] **Step 3: Run migration/tests and commit**

```bash
git add backend/app/modules/points backend/alembic/versions/0005_points_rewards.py backend/tests/integration/points
git commit -m "feat: add immutable points and reward persistence"
```

### Task 2: Implement Ledger Posting and Wallet Projection

**Files:**
- Create: `backend/app/modules/points/ledger_service.py`
- Create: `backend/tests/integration/points/test_ledger_service.py`

**Interfaces:**
- Produces:
  - `post_entry(command: PostLedgerEntry) -> PointsLedger`
  - `get_spendable_points(user_id) -> int`
  - `grant_assignment_reward(claim_id, user_id, amount, ranking_effective_at) -> PointsLedger`

- [ ] **Step 1: Write failing idempotency test**

Call `grant_assignment_reward` twice for the same Claim. Assert one ledger entry and one wallet balance increment.

- [ ] **Step 2: Write rollback test**

Force wallet projection failure after ledger insert and assert the transaction rolls back both.

- [ ] **Step 3: Implement append-only service**

Never update or delete Ledger amount. Wallet row is locked with `FOR UPDATE` for balance-changing operations.

- [ ] **Step 4: Run tests and commit**

```bash
git add backend/app/modules/points/ledger_service.py backend/tests/integration/points/test_ledger_service.py
git commit -m "feat: post points ledger entries atomically"
```

### Task 3: Wire Submission Approval to Assignment Reward

**Files:**
- Modify: `backend/app/modules/submissions/review_service.py`
- Create: `backend/tests/integration/points/test_submission_reward.py`

**Interfaces:**
- Consumes `LedgerService.grant_assignment_reward`.
- Produces atomic approval + reward behavior.

- [ ] **Step 1: Write concurrent reviewer test**

Two authorized reviewers approve the same Submission concurrently. Assert:
- Claim COMPLETED once;
- one Assignment reward ledger entry;
- wallet increments once;
- second call returns idempotent result or `ALREADY_REVIEWED`.

- [ ] **Step 2: Implement approval transaction boundary**

Use the same database transaction/session for Claim lock, Submission APPROVED, reward lock CONFIRMED, ledger posting, and Assignment COMPLETED.

- [ ] **Step 3: Run tests and commit**

```bash
git add backend/app/modules/submissions/review_service.py backend/tests/integration/points/test_submission_reward.py
git commit -m "feat: issue task rewards exactly once"
```

### Task 4: Implement Reward Redemption Reservations

**Files:**
- Create: `backend/app/modules/points/redemption_service.py`
- Create: `backend/tests/integration/points/test_redemption_concurrency.py`

**Interfaces:**
- Consumes `AcademicTermProvider.current_term_key() -> str`. Plan 05 defines the Protocol plus a test/static provider; Plan 08 wires it to the audited `CURRENT_ACADEMIC_TERM` system setting.
- Produces:
  - `request_redemption(user_id, reward_item_id) -> RewardRedemption`
  - `approve_redemption(actor, redemption_id) -> RewardRedemption`
  - `reject_redemption(actor, redemption_id, reason) -> RewardRedemption`
  - `fulfill_redemption(actor, redemption_id, note) -> RewardRedemption`

- [ ] **Step 1: Write double-spend test**

Seed wallet 1500. Launch two concurrent requests for 1000-point items. Assert at most one active reservation and spendable never below zero.

- [ ] **Step 2: Write last-stock test**

RewardItem stock=1. Two users request concurrently. Exactly one succeeds; stock reservation count is one.

- [ ] **Step 3: Implement locked reservation transaction**

Lock wallet and RewardItem rows. Validate enabled/time/user-term-limit. Read `AcademicTermProvider.current_term_key()` (for example `2026-fall`) and snapshot it on the Redemption. Count per-user limits by `(user_id, reward_item_id, term_key)`; changing the current term later never changes historical rows. If the provider returns empty/invalid term key, fail the request with a configuration error instead of silently using a calendar date. Reserve points and one stock unit atomically.

- [ ] **Step 4: Implement approve/reject/fulfill**

APPROVE turns reservation into a negative `REWARD_REDEMPTION` ledger entry and keeps stock committed. REJECT releases points and stock. FULFILL is idempotent and records fulfillment metadata.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/app/modules/points/redemption_service.py backend/tests/integration/points/test_redemption_concurrency.py
git commit -m "feat: reserve points and reward inventory atomically"
```

### Task 5: Implement Reward Reversal

**Files:**
- Modify: `backend/app/modules/points/ledger_service.py`
- Create: `backend/tests/integration/points/test_reward_reversal.py`

**Interfaces:**
- Produces `reverse_assignment_reward(actor, ledger_id, reason) -> PointsLedger`.

- [ ] **Step 1: Write reversal tests**

Original +200 reward:
- reversal produces new -200 entry linked by `reversal_of_id`;
- original row remains unchanged;
- wallet decreases by 200;
- second reversal request is idempotent/rejected;
- reversal requires reason and authorized actor.

- [ ] **Step 2: Preserve ranking effective period**

Set reversal `ranking_effective_at` equal to original reward effective instant.

- [ ] **Step 3: Run and commit**

```bash
git add backend/app/modules/points/ledger_service.py backend/tests/integration/points/test_reward_reversal.py
git commit -m "feat: reverse task rewards without mutating ledger history"
```

### Task 6: Implement Ranking Projection and Rebuild

**Files:**
- Create: `backend/app/modules/rankings/service.py`
- Create: `backend/app/modules/rankings/redis_projection.py`
- Create: `backend/app/workers/jobs/rebuild_rankings.py`
- Create: `backend/tests/integration/rankings/test_rankings.py`
- Create: `backend/tests/workers/test_ranking_rebuild.py`

**Interfaces:**
- Produces:
  - `RankingService.top(period, limit)`
  - `RankingService.around_me(user_id, period, radius)`
  - `rebuild_all_rankings()`.

- [ ] **Step 1: Write daily/monthly/all-time tests**

Seed ledger entries across UTC dates that map to different BUSINESS_TIMEZONE dates. Assert correct local-day/month scores.

- [ ] **Step 2: Write August-reward/September-reversal test**

Original reward effective in August, reversal posted in September. Assert August score is corrected and September score is unaffected.

- [ ] **Step 3: Implement PostgreSQL aggregation query**

Aggregate only `affects_ranking=true` by `ranking_effective_at` converted to business period.

- [ ] **Step 4: Implement Redis Sorted Set projection**

Keys exactly:
- `ranking:daily:<YYYY-MM-DD>`
- `ranking:monthly:<YYYY-MM>`
- `ranking:all`

- [ ] **Step 5: Write Redis-loss rebuild test**

Flush Redis, run rebuild, compare Top N and around-me result before/after.

- [ ] **Step 6: Run and commit**

```bash
git add backend/app/modules/rankings backend/app/workers/jobs/rebuild_rankings.py backend/tests
git commit -m "feat: project and rebuild CampusQuest rankings"
```

### Task 7: Implement Honors

**Files:**
- Create: `backend/app/modules/rankings/honor_models.py`
- Create: `backend/app/modules/rankings/honor_service.py`
- Create: `backend/alembic/versions/0006_honors.py`
- Create: `backend/tests/unit/rankings/test_honors.py`

**Interfaces:**
- Produces `Honor`, `UserHonor`, `evaluate_honors(user_id, event)`.

- [ ] **Step 1: Write fixed-rule tests**

Cover `TOTAL_COMPLETED`, `ON_TIME_STREAK`, `DAILY_RANK`, `MONTHLY_RANK`, `TOTAL_EARNED_POINTS`.

- [ ] **Step 2: Write period-honor idempotency test**

Same user evaluated twice for `2026-09 MONTHLY_RANK=1` receives one UserHonor only.

- [ ] **Step 3: Implement display honor selection**

User may choose one owned Honor; reject selecting an honor not owned.

- [ ] **Step 4: Run and commit**

```bash
git add backend/app/modules/rankings backend/alembic/versions/0006_honors.py backend/tests/unit/rankings
git commit -m "feat: award and display CampusQuest honors"
```

### Task 8: Implement Personal Growth Queries and Points/Ranking APIs

**Files:**
- Create: `backend/app/modules/points/router.py`
- Create: `backend/app/modules/rankings/router.py`
- Create: `backend/app/modules/rankings/growth_service.py`
- Create: `backend/tests/integration/rankings/test_growth_api.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces points balance, rewards, redeem, rankings, around-me, honors, and growth endpoints.

- [ ] **Step 1: Write growth-metric test**

Seed completed Claims including one invalidated empty-shell then valid late submission. Assert on-time ratio uses final valid reward-lock timing, not invalidated shell.

- [ ] **Step 2: Implement growth query**

Return month points/rank, total earned points, completed count, on-time ratio, current streak, best monthly rank, honors.

- [ ] **Step 3: Run module gate**

```bash
cd backend
pytest tests/integration/points tests/integration/rankings tests/unit/rankings tests/workers/test_ranking_rebuild.py -v
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/modules/points/router.py backend/app/modules/rankings backend/app/main.py backend/tests
git commit -m "feat: expose points rewards rankings and growth APIs"
```
