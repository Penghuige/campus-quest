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

- [ ] Rebase onto latest main after PR #6 merges.
- [ ] Run current app against seeded E2E world.
- [ ] Capture baseline screenshots for the visual acceptance list.
- [ ] Record the 5 most visibly weak screens before changing code.

Expected output: a short PR comment with baseline screenshots and the exact screens chosen for first-pass redesign.

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

Work:

- [ ] desktop sidebar;
- [ ] shared route item primitive;
- [ ] active/hover/focus states;
- [ ] top context/header grammar;
- [ ] Student narrow bottom navigation;
- [ ] Teacher/Admin narrow fallback.

Constraints:

- no route changes;
- notification/profile actions remain reachable;
- no hidden permission bypasses;
- no E2E behavior changes.

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
- [ ] PR screenshot comparison posted.

Only then mark the visual-refresh PR Ready.
