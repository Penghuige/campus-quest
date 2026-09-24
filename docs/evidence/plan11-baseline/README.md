# Plan 11 baseline evidence — Task 1 (§17.0 matrix)

Captured **before any visual code** at branch `design/visual-refresh-v1`
(includes the merge of `main` @ `800cfb7`; no visual changes applied).

## Matrix (brief §17.0)

| Axis | Value |
|---|---|
| Browser | Chromium (Playwright 1.63, headless) |
| Viewports | desktop `1440x900`, mobile `375x812` |
| Reduced motion | one extra mobile pass (`prefers-reduced-motion: reduce`) |
| World | fresh Plan 10 browser world per pass (same factories, same accounts, same operational state; deadlines derive from claim time, so relative urgency is equivalent across passes) |
| Tool | `frontend/e2e/visual-capture.spec.ts` (committed; re-runnable for every later "after" pass) |
| Manifest | every shot records name / route / account / viewport / motion / world run: `manifest.json` alongside this README (in the capture dir, not committed; regenerable) |

World run ids: desktop `e162391439b8`, mobile `c0f1f049de08`,
mobile-reduced-motion `cf6aaa904f98` (each Playwright invocation seeds
its own world via `global-setup.ts`).

Regenerate (from `frontend/`, ports clear of any dev stack):

```bash
CQ_E2E=1 CQ_E2E_CAPTURE_DIR=/tmp/cq-baseline CQ_E2E_VIEWPORT=desktop \
CQ_E2E_BASE_URL=http://localhost:3100 \
CQ_E2E_API_URL=http://localhost:8100/api/v1 \
npx playwright test visual-capture.spec.ts
# repeat with CQ_E2E_VIEWPORT=mobile, then + CQ_E2E_REDUCED_MOTION=1
```

Full set: 19 screens × 3 passes = 57 captures. The five screens below
are committed here as the durable baseline record (the full set
regenerates with the command above).

## The 5 weakest screens (first-pass redesign targets)

Grounded in a vision-model audit of the desktop captures plus the
brief §1 diagnosis; both independently converge.

### 1. Student dashboard (`/`)

- no "current priority / next action" hero — the greeting block and
  stat pills carry no action path;
- three near-identical stat pills + one wrapped pill (累计获得) — equal
  visual weight everywhere;
- bottom "最新任务" grid renders **list bullets leaking outside the
  cards** — a real pre-existing CSS bug (cards rendered as list items),
  to be fixed with the task-card pass;
- same task appears multiple times across 我的任务/最新任务 sections
  (presentation-level dedup consideration, no API change).

### 2. Task square (`/tasks`)

- uniform card clones, no quota/claimed state on cards, no visible
  claim affordance or filters;
- badge soup (rarity + points + deadline + platform all mid-weight);
- same leaked-bullet bug as the dashboard grid.

### 3. Teacher review queue (`/teacher/reviews`)

- single-card queue with ~80% dead space in the empty detail pane —
  no master/detail, no table density, no filter toolbar;
- action buttons cluster at card bottom, not a stable action bar.

### 4. Admin users (`/admin/users`)

- UUID column wraps to 2-3 lines, tripling row height;
- oversized native selects in the toolbar; stacked per-row action
  buttons; near-monochrome status chips with no semantic weight.

### 5. Auth login (`/login`)

- single centered card, ~70% dead space at 1440px, zero brand
  character (no split shell, no visual identity).

## Cross-cutting findings (all five screens)

- no sidebar anywhere — one flat horizontal top nav for every role
  (the shell the plan replaces);
- navigation active pill and in-content status chips share the same
  light-blue — competing signals;
- borders on everything: every region is the same bordered white
  panel (the brief's card-wall diagnosis, confirmed).
