# CampusQuest 13 Student Secondary Surfaces Polish Implementation Plan

> Status: Completed & merged (PR #23, 2026-10-06) — historical record, not an execution authorization.
> File/numbering drift: migration numbers and file names in task text may differ from what actually landed — the migration chain in backend/alembic/versions/ is authoritative.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Numbering note: plan-12 reserved "plan-13" for the Tailwind/Radix migration; that migration is renumbered to **plan-14** so this visible-polish workstream lands first (owner direction 2026-10-05: "进行下一步" after asking why the UI looked unchanged).

**Goal:** make the four student secondary surfaces — 积分兑换 Rewards, 排行榜 Rankings, 社区 Community (task-detail comments), 通知 Notifications — visibly polished under the INK identity, executing plan-11 Task 6 with its owner P2 rulings.

**Architecture:** presentation-only redesign of four feature views against the existing global classes and `components/ui` primitives; every surface ships with before/after §17.0 screenshots and an updated pixel-regression baseline (dogfooding the plan-12 harness). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`; governing plan `docs/superpowers/plans/2026-09-24-campusquest-11-visual-refresh.md` (Task 6 + the 2026-09-24 mid-pass P2 rulings quoted below); design sources `docs/quality/frontend-design-system.md`, `docs/quality/frontend-patterns.md`; harness `frontend/scripts/check-css-integrity.mjs`, `frontend/e2e/visual-regression.spec.ts`.

**Current-state evidence (main @ 0bbf33e baselines):**
- Rewards: three equal-weight balance numbers (可用/累计/可花费 100/100/100), no next-reward progress; reward shelf = one bare white card (title/desc/cost/full-width button); content hugs a narrow column leaving the right half of a 1440px viewport empty.
- Rankings: period tabs OK; zero top-three distinction; current-user anchor = tiny 我 badge; 排行榜 and 我的附近 sections visually identical plain strips; same narrow-column emptiness.

## Global Constraints

- Presentation-only. No API shape changes, no backend changes, no business semantics (G13). If a surface seems to need data the DTO does not expose, render what exists and record the gap in the PR — do not extend the API in this workstream.
- Selector contract `docs/quality/e2e-selector-contract.md`: Class A frozen; Class B only equal-strength replacement in the same commit.
- plan-11 P2 rulings for Task 6 (owner, 2026-09-24 — verbatim requirements):
  - Rewards/growth leave the equal-metric grammar: **one dominant spendable balance + next-reward progress**; earned/frozen/debt quiet; reward tiles with icon/cost/availability/CTA; growth = one 本月 story + compact secondary.
  - Rankings: **restrained top-3 distinction + strong 我 anchor** (no podium).
  - Notifications: spacing/unread weight/event grouping; no new badges.
- Cross-page rule before styling EVERY surface: (1) what should the user notice first? (2) what must be read before the primary action? (3) identity/context vs semantic status — never the same visual treatment; (4) what can become quiet metadata instead of another badge/card/panel?
- Tokens OKLCH in `:root`; no raw hex/oklch outside custom-property declarations; no `!important`; `npm run check:css` green after every commit; new component rules placed BEFORE the consolidated reduced-motion block in globals.css; honor reduced-motion for any new transition.
- Rarity/gamification accents stay local; restrained academic-productivity personality (design-system §2); no wall of equal cards; destructive actions stay visibly distinct.
- Every task: `check:css` + typecheck + lint + unit + build green; zero-skip Playwright for touched surfaces; before/after screenshots under the §17.0 matrix; pixel baselines regenerated via `CQ_VISUAL_UPDATE=1` and re-reviewed in the same PR (baselines are reviewed like code).
- Read `frontend/node_modules/next/dist/docs/` before relying on any Next.js behavior.

---

### Task 0: Before-evidence + weakest-element list

**Files:**
- Create: `docs/evidence/plan13-before/README.md` (+ PNGs, not committed — PNGs live in the PR description; commit only the README listing them)

- [ ] **Step 1:** Extend `frontend/e2e/visual-capture.spec.ts`'s screen list with `student-notifications` (/notifications) and `student-task-detail` community section (scroll-anchor shot of `.comment-thread` on the open task) IF not already covered; run the harness desktop + mobile (+ one reduced-motion pass) per the §17.0 matrix with the stack up.
- [ ] **Step 2:** Write the weakest-element list (max 8 items across the 4 surfaces, each with a one-line rationale tied to a plan-11 P2 ruling) into `docs/evidence/plan13-before/README.md`; paste the PNGs into the PR description.
- [ ] **Step 3:** Commit `docs(plan13): before-evidence inventory for student secondary surfaces`.

### Task 1: Rewards （积分兑换） hierarchy rebuild

**Files:**
- Modify: `frontend/src/features/rewards/RewardsView.tsx` (composition only — verify the real filename first)
- Modify: `frontend/src/app/globals.css`
- Test: `frontend/src/__tests__/reward-view.test.ts` (extend if view-model logic changes)
- Baselines: `frontend/e2e/visual-regression.spec.ts-snapshots/student-rewards-linux.png` (regenerate)

**Interfaces:**
- Consumes: existing rewards view-model DTO (balance/earned/spendable, reward items with cost/stock/availability) — no new fields.
- Produces: `.balance-hero`, `.balance-quiet`, `.reward-tile`, `.reward-tile-icon`, `.reward-tile-cta` classes (names adjusted to whatever already exists — reuse first per design-system §16).

- [ ] **Step 1:** Balance section → ONE dominant spendable number (display scale, tabular numerals) + next-reward progress strip (cost, remaining points to next affordable item, progress bar); 累计获得/冻结/负债 become quiet metadata row.
- [ ] **Step 2:** Reward shelf → tile grid (≥2 columns at 64rem+) with per-tile: restrained icon tile (existing `navIcons` glyph or first-character monogram — no new assets), name, cost as primary signal, availability/stock as quiet metadata, state-appropriate CTA （兑换 primary / 积分不足 disabled-with-reason / 缺货 quiet). Not a wall of equal cards: shelf tiles are discrete objects, sized by content.
- [ ] **Step 3:** Content uses the viewport: grid max-width consistent with `.task-detail`'s 56rem step at 48rem+; no narrow orphan column on desktop.
- [ ] **Step 4:** Mobile: balance hero compresses to one row; tiles go single column; CTA stays ≥44px target.
- [ ] **Step 5:** Gates: unit tests green (extend `reward-view.test.ts` for any new derived presentation values — presentation-only derivation, backend stays authoritative); `check:css`; zero-skip Playwright student suites; before/after captures desktop+mobile; regenerate `student-rewards` baseline and eyeball it.
- [ ] **Step 6:** Commit `feat(plan13): rewards surface — dominant balance + next-reward progress + reward tiles`.

### Task 2: Rankings （排行榜） top-3 + 我 anchor

**Files:**
- Modify: `frontend/src/features/rankings/` view component (verify filename)
- Modify: `frontend/src/app/globals.css`
- Test: existing rankings view test
- Baselines: `student-rankings-linux.png` (regenerate)

- [ ] **Step 1:** Top-3 rows get restrained distinction (rank numeral weight + subtle keyline tint — gold/amber reserved-adjacent tone per rarity Legendary token is ALLOWED here as a local accent; no podium, no badges explosion).
- [ ] **Step 2:** 我 anchor: current-user row pinned visually (surface-brand background + strong keyline + 我 chip already present — strengthen to design-system "current-user highlight consistent") and, when the user is outside the top list, the 我的附近 section must read as one continuous story around the user (above/below rows, user row centered and highlighted).
- [ ] **Step 3:** Period tabs: keep behavior; visual = segmented control consistent with the shell grammar.
- [ ] **Step 4:** Honor display: if the DTO carries honor/称号 fields render them as quiet text next to nicknames; if not, record the gap in the PR and render nothing (no placeholder).
- [ ] **Step 5:** Gates identical to Task 1 Step 5 (rankings test, captures, baseline regenerate).
- [ ] **Step 6:** Commit `feat(plan13): rankings surface — restrained top-3 + strong current-user anchor`.

### Task 3: Community (task-detail comments) hierarchy

**Files:**
- Modify: `frontend/src/features/community/CommentThread.tsx`, `CommentComposer.tsx`, `Reactions.tsx` (composition only)
- Modify: `frontend/src/app/globals.css`
- Baselines: `student-task-detail-linux.png` (regenerate — community lives on task detail)

- [ ] **Step 1:** Comment rows: author/anonymous label + timestamp/edited marker as quiet metadata line; content comfortable reading width; vote/reaction controls compact and right-aligned or inline-quiet; deleted tombstones visually quieter but legible.
- [ ] **Step 2:** Anonymous mode state must be explicit before posting (verify existing copy; presentation may emphasize, not rewrite Class A copy).
- [ ] **Step 3:** Composer: primary action obvious, helper text quiet; no new badges.
- [ ] **Step 4:** Gates identical to Task 1 Step 5 (community e2e suite zero-skip; regenerate task-detail baseline).
- [ ] **Step 5:** Commit `feat(plan13): community surface — author/content/action hierarchy`.

### Task 4: Notifications （通知） scanability

**Files:**
- Modify: `frontend/src/features/notifications/` view component (verify filename)
- Modify: `frontend/src/app/globals.css`
- Modify: `frontend/e2e/visual-regression.spec.ts` (add `student-notifications` shot with per-shot masks — notification timestamps are clock-volatile)
- Baselines: add `student-notifications-linux.png`

- [ ] **Step 1:** Unread weight via typography + subtle surface tint (not heavy borders); read = quiet.
- [ ] **Step 2:** Event-type visual anchor: small glyph or keyline per category (review result / deadline / redemption / system) — restrained, text always present.
- [ ] **Step 3:** Grouping/spacing rhythm; long content truncates gracefully; empty state unchanged (already good).
- [ ] **Step 4:** Gates identical to Task 1 Step 5; add the new shot to the suite with timestamp masks + engagement assertions; two consecutive suite runs green (cross-run stability).
- [ ] **Step 5:** Commit `feat(plan13): notifications surface — unread hierarchy + event anchors`.

### Task 5: Fold-back + merge gate (G17)

- [ ] **Step 1:** Any NEW visual grammar that emerged (balance-hero pattern, top-3 treatment, event anchors) gets folded into `docs/quality/frontend-design-system.md` (and `frontend-patterns.md` if archetypes changed) in the same branch — no merge-carry documentation.
- [ ] **Step 2:** Full gate: `make verify` frontend portions + zero-skip Playwright full suite + `make visual-regression` green on final baselines + `check:css` + build.
- [ ] **Step 3:** PR carries: before/after §17.0 pairs for all four surfaces (desktop + mobile), the weakest-element list's resolution status per item, regenerated baselines list.
- [ ] **Step 4:** Commit `docs(plan13): fold accepted grammar into durable design sources`.

## Self-review record

- Spec coverage: plan-11 T6 four surfaces each map to a task (Rewards→T1, Rankings→T2, Community→T3, Notifications→T4); evidence discipline→T0; fold-back→T5. plan-11 T7/T8/T9 (Teacher/Admin/auth) and the Tailwind migration (plan-14) are explicitly OUT of scope here.
- Placeholder scan: visual redesign tasks specify hierarchy contracts + acceptance evidence rather than verbatim CSS — the repo's established convention for design execution (plan-11 precedent); every mechanical step (gates, baselines, commits) is exact.
- Type consistency: class names proposed in T1 are "reuse-first" placeholders resolved at implementation; no cross-task interface dependencies beyond the shared harness.
