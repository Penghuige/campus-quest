# CampusQuest 11 Visual Refresh Implementation Plan

> This is a post-Plan-10 presentation-only workstream.
>
> Work on branch `design/visual-refresh-v1`. Keep the PR Draft until Plan 10 / PR #6 is merged and this branch is rebased onto latest `main`.

**Goal:** substantially improve CampusQuest visual quality with minimal business-code churn.

**Primary brief:** `docs/quality/frontend-visual-refresh.md`

**Existing constraints:** `docs/quality/frontend-design-system.md`, `docs/quality/frontend-patterns.md`, `AGENTS.md`.

## Global constraints

- No business semantic changes.
- No backend changes unless a purely presentational API gap is proven and owner-approved.
- Preserve accessibility baseline.
- Keep selectors/test IDs stable where practical.
- Do not replace the working frontend architecture with a new framework or UI stack.
- Prefer CSS/token/shell/component changes over feature rewrites.
- Use actual screenshots for visual review; do not accept only “tests pass” as design evidence.

## Task 1 — Establish before/after evidence

- [x] Rebase onto latest main after PR #6 merges (merged `main` @ `800cfb7` in — no force-push on the Draft branch).
- [x] Run current app against seeded E2E world.
- [x] Capture baseline screenshots for the visual acceptance list,
      under the brief §17.0 evidence matrix (Chromium; 375x812 and
      1440x900; same seeded world; named route/account/state; fixed
      clock for deadline/countdown screens; one reduced-motion pass).
- [x] Record the 5 most visibly weak screens before changing code.

Expected output: a short PR comment with baseline screenshots and the exact screens chosen for first-pass redesign.

## Task 1b — Freeze the E2E selector contract

The Plan 10 browser suite does not rely only on `data-testid`; it pins
behavior through role/name/label selectors plus structural hooks. A
visual shell rewrite is exactly where those locators move, so this task
runs **after the Task 1 rebase and before any visual code (Task 2+)**:

- [x] Inventory every Plan 10 Playwright locator/assertion for each
      screen the refresh will touch. Known structural hooks to start
      from: `.task-card`, `.claim-panel`, `.deadline-line`,
      `.page-head`, `.review-item`, `.review-pair`, `.review-tier`,
      dialog ids / accessible labels, and exact action copy.
- [x] Classify each as **behavior/accessibility contract** (roles,
      accessible names, labels, dialog semantics, asserted copy,
      business/privacy/RBAC assertions) vs **pure structural locator**.
- [x] Record the inventory in the PR (comment or committed doc table)
      so reviewers can diff against it.
- [ ] Implementation rule: behavior/accessibility contracts are
      preserved as-is; a pure structural locator may only be replaced
      by an equally strong locator/assertion — never delete or weaken
      a business/privacy/RBAC assertion to fit the new DOM.
- [ ] Gate cadence: zero-skip Playwright after every major surface
      batch (shell, Student milestone, Teacher, Admin, auth), and the
      full fresh `make release-gate` after the shell, after the
      Student milestone, after Teacher/Admin, and at the end — not
      only once at the end.

## Task 2 — Refresh tokens and global visual rhythm

Files likely involved:

- `frontend/src/app/globals.css`
- `frontend/src/lib/designTokens.ts`

Work:

- [ ] soften background/border contrast;
- [ ] add raised/brand surface tokens if needed;
- [ ] revise radius/shadow hierarchy;
- [ ] revise page/section typography scale;
- [ ] add motion tokens;
- [ ] keep WCAG AA contrast.

Acceptance:

- auth, Student, Teacher, Admin all still readable before feature-specific CSS lands;
- no raw color sprawl.

## Task 3 — Build the navigation shell

### Frozen navigation contract (normative — no Agent discretion)

Routes do not change; this contract freezes reachability and hierarchy.

**Student desktop sidebar** (8 items, two groups):

- primary: 首页 Home · 任务 Tasks · 我的任务 Claims · 排行榜 Ranking · 积分奖励 Rewards · 社区 Community
- secondary (bottom of sidebar): 通知 Notifications · 我的 Profile

**Student bottom navigation** (exactly 5 slots, narrow only):

首页 Home · 任务 Tasks · 我的任务 Claims · 排行榜 Ranking · 我的 Profile

**Overflow destinations** (reachable, not in bottom nav):

- 通知 Notifications — top-bar bell with unread badge (always visible), plus Profile entry;
- 积分奖励 Rewards — entry on the Profile screen;
- 社区 Community — entry on the Profile screen.

**Teacher/Admin narrow:** hamburger menu sheet containing the *full*
staff navigation list (nothing is demoted beyond opening the sheet);
content stays list/table-first; no bottom navigation.

**Active state:** exactly one item per nav landmark carries
`aria-current="page"`; its visual state (brand-tinted background +
keyline) must be distinguishable from hover and from focus-visible.

**Keyboard/focus:** nav items are real links in standard tab order;
focus-visible ring ≥ 2px with AA contrast; the staff menu sheet traps
focus and closes on Escape; the bottom nav is a `<nav>` landmark with
plainly focusable links (no JS-only activation).

**Safe-area rule:** the bottom nav's own padding includes
`env(safe-area-inset-bottom)`; every page under the Student shell gets
content `padding-bottom` ≥ (bottom-nav total height including inset) +
16px, so the fixed nav can never cover a primary action.

### Work

- [ ] desktop sidebar;
- [ ] shared route item primitive;
- [ ] active/hover/focus states per the contract above;
- [ ] top context/header grammar;
- [ ] Student narrow bottom navigation per the 5-slot contract;
- [ ] Teacher/Admin narrow menu sheet per the contract.

Constraints:

- no route changes;
- notification/profile actions remain reachable (per the overflow contract);
- no hidden permission bypasses;
- no E2E behavior changes (Task 1b inventory governs selector moves).

## Task 4 — Student dashboard first-pass redesign

This is the visual anchor.

Work:

- [ ] “current priority / next action” hero;
- [ ] compact points + reward progress;
- [ ] ranking/around-me summary;
- [ ] active claims / revisions;
- [ ] task discovery;
- [ ] recent notifications.

Avoid equal KPI card walls.

Capture desktop + 375x812 before/after screenshots in PR.

## Task 5 — Task + Claim + Submission surfaces

- [ ] improve task card hierarchy;
- [ ] improve task detail metadata;
- [ ] add state-progress/timeline presentation for Claim/Submission;
- [ ] redesign validation report hierarchy;
- [ ] keep upload semantics and selectors stable;
- [ ] ensure deadline/reward urgency remains backend-authoritative.

## Task 6 — Rewards, Rankings, Community, Notifications

Rewards:
- [ ] stronger reward item visual hierarchy;
- [ ] restrained visual identity per item/category;
- [ ] clear cost/action state.

Rankings:
- [ ] top results + around-me hierarchy;
- [ ] current-user anchor;
- [ ] restrained top-three treatment.

Community:
- [ ] stronger author/content/action hierarchy;
- [ ] anonymous state remains explicit;
- [ ] reactions compact.

Notifications:
- [ ] event-type visual anchors;
- [ ] read/unread hierarchy without heavy borders.

## Task 7 — Teacher workstation

- [ ] dense desktop shell;
- [ ] review queue master/detail;
- [ ] clear selected state;
- [ ] task/import tables;
- [ ] filter/action toolbar;
- [ ] narrow fallback.

Do not convert operational tables into decorative cards.

## Task 8 — Admin workstation

- [ ] users/whitelist;
- [ ] rewards/redemptions;
- [ ] settings;
- [ ] audit;
- [ ] repair operations.

Design goal: serious, high-scan-density, predictable action placement.

## Task 9 — Auth polish

- [ ] wide split-shell treatment;
- [ ] narrow single-card treatment;
- [ ] student login/register;
- [ ] staff login/invitation/TOTP;
- [ ] preserve current security/error semantics.

Optional: add one lightweight local illustration only if it clearly improves the screen and has repository-safe licensing/origin.

## Task 10 — Shared state polish

- [ ] loading skeletons shaped like target content;
- [ ] empty states with restrained icon/next action;
- [ ] error surfaces less visually heavy while preserving request IDs;
- [ ] dialogs/popovers/drawers consistent;
- [ ] focus states verified.

## Task 11 — Visual regression pass

All captures use the brief §17.0 evidence matrix (Chromium; 375x812 +
1440x900; the same seeded Plan 10 world used for the Task 1 baseline;
named route/account/state; fixed clock for deadline/countdown screens;
one reduced-motion pass). Before/after pairs must come from the same
world and state.

Required screenshots:

- Student dashboard desktop/mobile;
- task list/detail;
- claim/submission;
- ranking/rewards;
- community/notifications;
- Teacher review;
- Admin dense table;
- login/TOTP.

Review each for:

- visual hierarchy;
- density;
- long Chinese text;
- keyboard focus;
- 200% zoom;
- mobile tap targets;
- loading/empty/error states.

## Task 12 — Final low-risk merge gate

- [ ] diff review: presentation-only changes;
- [ ] generated API contracts unchanged unless deliberately required;
- [ ] frontend typecheck;
- [ ] frontend lint;
- [ ] frontend unit tests;
- [ ] production build;
- [ ] Playwright;
- [ ] fresh `make release-gate`;
- [ ] PR screenshot comparison posted;
- [ ] **durable design-source migration:** fold every accepted Plan 11
      rule back into `frontend-design-system.md` (token/surface/motion
      grammar) and update `frontend-patterns.md` where the shell or
      page archetypes changed; mark the refresh brief as absorbed so
      `AGENTS.md`-directed agents cannot reintroduce the old visual
      grammar (frontend G17 — no merge-carry documentation).

Only then mark the visual-refresh PR Ready.
