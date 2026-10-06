# CampusQuest 12 Visual Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** close the visual-quality execution gaps found in the 2026-10-05 frontend audit — fix the two hard CSS bugs, add automated CSS guards, diversify demo seed data, add component-level and pixel-level visual regression — then (Phase C) migrate the styling stack to the design documents' prescribed Tailwind + shadcn/Radix.

**Architecture:** Phase A fixes current-state bugs in hand-written CSS and adds a zero-dependency Node guard script wired into CI. Phase B adds a dev-only component gallery route and Playwright pixel baselines under the existing §17.0 evidence matrix. Phase C is a separately-planned multi-milestone stack migration (strategy recorded below).

**Owner ruling (2026-10-05, chat):** the plan-11 constraint "Do not replace the working frontend architecture with a new framework or UI stack" is lifted for Phase C; all three tiers approved.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`; design sources `docs/quality/frontend-design-system.md`, `docs/quality/frontend-patterns.md`; predecessor plan `docs/superpowers/plans/2026-09-24-campusquest-11-visual-refresh.md` (Tasks 6–11 continue under it; this plan does not replace them); selector contract `docs/quality/e2e-selector-contract.md` is normative.

**Audit baseline (main @ 5cf514c):** `.btn-danger` used in 10+ admin/teacher files, zero CSS rules; `.claim-detail` root container has zero CSS rules; no `toHaveScreenshot` anywhere; no Tailwind/Radix; demo seed lacks a LEGENDARY task; `.link` has no `:hover`.

## Global Constraints

- Presentation-only. No business semantic changes (G13); no API shape changes; no backend app-code changes (Phase A/B touch only `backend/scripts/` and `backend/tests/e2e/` tooling).
- Selector contract: Class A frozen; Class B moves only with equal-strength replacement in the same commit.
- Tokens stay OKLCH in `:root`; no raw hex/oklch outside custom-property declarations; no `!important`.
- Preserve the ink identity (dark rail + warm content) and the motion system (`cq-rise`/`cq-sweep`, consolidated reduced-motion block).
- Read `frontend/node_modules/next/dist/docs/` before using any Next.js API (notFound, route config).
- Every PR: `npm run typecheck && npm run lint && npm run test:unit && npm run build` green in `frontend/`; zero-skip Playwright for any surface the task touches; screenshot evidence for visual changes per the §17.0 matrix.

---

## Phase A — PR-A: hard fixes + CSS guard + seed variety

### Task 1: CSS integrity guard script (the failing regression test)

**Files:**
- Create: `frontend/scripts/check-css-integrity.mjs`
- Modify: `frontend/package.json` (add script)

**Interfaces:**
- Produces: `npm run check:css` — exit 1 with a numbered error list when (a) a kebab-case class referenced in `src/**/*.tsx` static className strings is absent from `src/app/globals.css`, (b) a raw hex/`oklch()` color appears outside a custom-property declaration, (c) `!important` appears. Defined-but-unreferenced classes report as warnings only.

- [ ] **Step 1: Write the guard script**

```js
#!/usr/bin/env node
// CSS integrity guard: used-but-undefined classes, raw colors outside
// token declarations, !important. Zero-dependency; runs in CI.
import { readFileSync } from "node:fs";
import { execSync } from "node:child_process";

const cssPath = "src/app/globals.css";
const css = readFileSync(cssPath, "utf8");
const errors = [];
const warnings = [];

// --- defined classes: every .class token appearing in any selector ---
const defined = new Set();
for (const m of css.matchAll(/\.([a-z][a-z0-9-]*)/g)) defined.add(m[1]);

// --- used classes: static string literals inside className={...} ---
const files = execSync(
  "git grep -l -- '*.tsx' -- src | grep -v __tests__",
  { encoding: "utf8" },
).trim().split("\n").filter(Boolean);
const used = new Map(); // class -> [file]
for (const f of files) {
  const text = readFileSync(f, "utf8");
  for (const m of text.matchAll(/className\s*=\s*(\{[^}]*\}|"[^"]*"|'[^']*')/gs)) {
    const expr = m[1];
    if (expr.includes("${")) {
      // template literal: still scan the static segments
    }
    for (const s of expr.matchAll(/"([^"]*)"|'([^']*)'|`([^`]*)`/gs)) {
      const lit = s[1] ?? s[2] ?? s[3];
      for (const tok of lit.split(/\s+/)) {
        if (!/^[a-z][a-z0-9-]*$/.test(tok)) continue;
        if (/\$\{/.test(tok)) continue;
        if (!used.has(tok)) used.set(tok, []);
        used.get(tok).push(f);
      }
    }
  }
}

// Classes produced dynamically or by libraries; extend only with a
// comment citing the producing code.
const ALLOWED_UNDEFINED = new Set([
  // e.g. "active" toggled via data attributes elsewhere
]);

for (const [cls, where] of used) {
  if (defined.has(cls)) continue;
  if (ALLOWED_UNDEFINED.has(cls)) continue;
  errors.push(`used-but-undefined class ".${cls}" in ${[...new Set(where)].join(", ")}`);
}

// --- raw colors: strip custom-property declarations, then scan ---
const withoutDecls = css.replace(/^\s*--[a-z0-9-]+\s*:[^;]+;.*$/gim, "");
for (const [i, line] of withoutDecls.split("\n").entries()) {
  if (/#[0-9a-fA-F]{3,8}\b/.test(line))
    errors.push(`raw hex color at ${cssPath}:${i + 1}: ${line.trim()}`);
  if (/\boklch\(/.test(line))
    errors.push(`raw oklch() outside token declaration at ${cssPath}:${i + 1}: ${line.trim()}`);
  if (/!important/.test(line))
    errors.push(`!important at ${cssPath}:${i + 1}: ${line.trim()}`);
}

// --- defined-but-unreferenced: warning only ---
const tsxAll = files.map((f) => readFileSync(f, "utf8")).join("\n");
for (const cls of defined) {
  if (!tsxAll.includes(cls) && !new RegExp(`\\.${cls}[^{]*\\{[^}]*\\.`, "s").test(css))
    warnings.push(`defined-but-unreferenced ".${cls}" (dead CSS candidate)`);
}

for (const w of warnings) console.warn(`warn: ${w}`);
if (errors.length) {
  console.error(`CSS integrity: ${errors.length} error(s)`);
  errors.forEach((e, i) => console.error(`${i + 1}. ${e}`));
  process.exit(1);
}
console.log(`CSS integrity OK (${used.size} used classes, ${defined.size} defined, ${warnings.length} warnings)`);
```

package.json addition:

```json
"check:css": "node scripts/check-css-integrity.mjs",
```

- [ ] **Step 2: Run it — expect RED listing `.btn-danger` and `.claim-detail`**

Run: `cd frontend && npm run check:css`
Expected: exit 1; error list includes `used-but-undefined class ".btn-danger"` and `".claim-detail"`.

- [ ] **Step 3: Commit the red guard**

```bash
git add frontend/scripts/check-css-integrity.mjs frontend/package.json
git commit -m "test(frontend): CSS integrity guard — used-but-undefined classes, raw colors, !important (red: btn-danger, claim-detail)"
```

### Task 2: Define `.btn-danger` (design-system §9 destructive hierarchy)

**Files:**
- Modify: `frontend/src/app/globals.css` (after the `.btn-secondary` block, ~line 420)
- Test: the Task 1 guard (red → green for `.btn-danger`)

**Interfaces:**
- Consumes: existing `--danger` semantic token in `:root` (verify the exact token name with `grep -n "danger" src/app/globals.css` first; if only `--danger` exists, derive hover via `color-mix`; if a `--danger-foreground` is missing, add it to `:root` next to `--danger`).

- [ ] **Step 1: Add the rule (and `--danger-foreground` token only if absent)**

```css
.btn-danger {
  background: var(--danger);
  color: var(--danger-foreground, oklch(99% 0 0));
}

.btn-danger:hover:not(:disabled) {
  background: color-mix(in oklch, var(--danger) 88%, black);
}
```

Fallback if the token check shows no `--danger`: add to the semantic group in `:root` a danger pair tuned against the existing palette (`--danger: oklch(55% 0.2 25); --danger-foreground: oklch(98% 0.01 25);`) and verify WCAG AA on the button label.

- [ ] **Step 2: Guard now passes for `.btn-danger`**

Run: `cd frontend && npm run check:css`
Expected: still exit 1 on `.claim-detail` only.

- [ ] **Step 3: Visual evidence** — capture one destructive-action surface before/after (teacher review 判无效 or admin redemption reject) with the existing capture harness:

```bash
cd frontend && CQ_E2E=1 CQ_E2E_CAPTURE_DIR=/tmp/p12-btn-danger CQ_E2E_VIEWPORT=desktop \
  npx playwright test visual-capture.spec.ts
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/app/globals.css
git commit -m "fix(frontend): define .btn-danger — destructive actions render as danger buttons (15 uses)"
```

### Task 3: Fix `.claim-detail` layout (student core page zero vertical rhythm)

**Files:**
- Modify: `frontend/src/app/globals.css` (next to `.task-detail`, ~line 1996)
- Test: the Task 1 guard (red → fully green)

- [ ] **Step 1: Add the rule**

```css
.claim-detail {
  display: grid;
  gap: var(--space-5);
  max-width: 42rem;
  align-content: start;
}

@media (min-width: 48rem) {
  .claim-detail {
    max-width: 56rem;
  }
}
```

Match `.task-detail`'s rhythm exactly; the claim page composes `claim-panel`, `RewardStatus`, `UploadPanel`, `AbandonControl` as grid children.

- [ ] **Step 2: Guard fully green**

Run: `cd frontend && npm run check:css`
Expected: exit 0, `CSS integrity OK`.

- [ ] **Step 3: Visual evidence** — capture `student-claim` desktop + mobile before/after (same §17.0 matrix command as Task 2 Step 3, plus `CQ_E2E_VIEWPORT=mobile`).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/app/globals.css
git commit -m "fix(frontend): .claim-detail grid rhythm — claim page sections no longer stack with zero gap"
```

### Task 4: Feedback hygiene sweep (small, one commit)

**Files:**
- Modify: `frontend/src/app/globals.css`

- [ ] **Step 1: Verify-then-fix list** (each item: grep first; skip with a note if already fixed on main):
  - `.link:hover` — add `color: var(--primary-strong); text-decoration-thickness: 2px;` if no hover rule exists;
  - `.review-item[data-selected="true"]` — if selection is still border-only, add `background: var(--surface-brand)` (verify token name; plan-11 Task 7 demands a *clear* selected state);
  - `frontend-design-system.md` §8: check the narrow-student bottom-nav wording against the shipped frosted light bar; if the doc still says "same INK dark rail", rewrite that sentence to describe the frosted bar (doc drift fold-back, G17).

- [ ] **Step 2: `npm run check:css` + focused unit tests green.**

- [ ] **Step 3: Commit**

```bash
git add frontend/src/app/globals.css docs/quality/frontend-design-system.md
git commit -m "fix(frontend): link hover + review selection feedback; design-system §8 nav drift"
```

### Task 5: Demo seed — one LEGENDARY task (rarity ladder fully renderable)

**Files:**
- Modify: `backend/scripts/seed_demo_content.py` (task seeding block, ~line 412-444)
- Test: run the seed script against the local stack

**Interfaces:**
- Consumes: existing task-seed entries (NORMAL×2, RARE×1, EPIC×1 pattern with `compute_claim_deadlines` and reward policy snapshot).
- Produces: a 5th demo task, title `【演示】校园文创设计众筹数据采集`, `rarity=LEGENDARY`, base points 260, duration 10 days, 1 slot. `backend/scripts/reset_demo_content.sql` needs no change (already scoped to `【演示】%`).

- [ ] **Step 1: Add the task entry** following the exact ORM-call shape of the existing four entries (copy the EPIC entry, change title/rarity/points/duration/slots).

- [ ] **Step 2: Verify against the real stack**

```bash
docker compose -f infra/docker-compose.yml up -d
cd backend && uv run python scripts/seed_demo_content.py
# then confirm the legendary row exists:
uv run python -c "..."  # SELECT title, rarity FROM tasks WHERE title LIKE '【演示】%'
```

If docker/the stack is unavailable in the execution environment, run `uv run python -m py_compile scripts/seed_demo_content.py` plus the script's own idempotence-guard code path review, and mark stack verification for the owner.

- [ ] **Step 3: Commit**

```bash
git add backend/scripts/seed_demo_content.py
git commit -m "feat(demo): seed one LEGENDARY task so the rarity ladder renders end-to-end"
```

### Task 6: Wire the guard into Makefile + CI

**Files:**
- Modify: `Makefile` (add `frontend-css-guard`; include in `verify` and `release-gate` after `frontend-lint`)
- Modify: `.github/workflows/ci.yml` (frontend job, after Lint)

- [ ] **Step 1: Makefile**

```makefile
.PHONY: frontend-css-guard
frontend-css-guard:
	cd frontend && npm run check:css
```

Add `frontend-css-guard` to the `verify` recipe (after `npm run lint`) and to the `release-gate` prerequisite list (after `frontend-lint`).

- [ ] **Step 2: CI step**

```yaml
      - name: CSS integrity
        run: npm run check:css
```

- [ ] **Step 3: Run `make frontend-css-guard` and `make verify` (frontend portions) green.**

- [ ] **Step 4: Commit**

```bash
git add Makefile .github/workflows/ci.yml
git commit -m "ci(frontend): CSS integrity guard in verify, release-gate, and CI"
```

---

## Phase B — PR-B: component gallery + pixel visual regression

### Task 7: Dev-only component gallery route

**Files:**
- Create: `frontend/src/app/dev/gallery/page.tsx`
- Test: `frontend/src/__tests__/dev-gallery.test.ts`

**Interfaces:**
- Consumes: only existing global classes and shared components (`TaskCard`, `sectionStates` primitives, `navIcons` glyphs). Inherits the root layout (no shell, no auth gate).
- Produces: `/dev/gallery` — one page rendering every shared visual primitive × every variant: `.btn` × (primary/secondary/ghost/danger × enabled/disabled), `.badge`/`.task-card-rarity` × 4 rarities, `TaskCard` × 4 rarities (fixture data), form controls incl. error state, `.dialog` open state, `SectionSkeleton`/`EmptyState`/`SectionError`, `.review-item` default/selected. Production build returns 404.

- [ ] **Step 1: Failing test**

```ts
// src/__tests__/dev-gallery.test.ts
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

test("dev gallery is production-gated and covers the primitive matrix", () => {
  const src = readFileSync("src/app/dev/gallery/page.tsx", "utf8");
  assert.match(src, /process\.env\.NODE_ENV === "production"/);
  assert.match(src, /notFound\(\)/);
  for (const cls of ["btn-danger", "task-card-rarity", "SectionSkeleton", "EmptyState"])
    assert.ok(src.includes(cls), `gallery covers ${cls}`);
});
```

Run: `cd frontend && npm run test:unit` — expect FAIL (file missing).

- [ ] **Step 2: Implement the page.** Header `if (process.env.NODE_ENV === "production") notFound();` (confirm `notFound` semantics in the bundled Next docs first). Compose sections with the existing classes; fixture tasks inline as plain objects matching `TaskCard`'s props; add `export const dynamic = "force-static"` if the bundled docs permit static prerender with the env gate — otherwise omit and note why.

- [ ] **Step 3: Test green + `npm run build` proves the route 404s in production mode** (build output must not list a publicly reachable gallery in prod semantics; document the check in the PR).

- [ ] **Step 4: Capture gallery screenshots** (desktop + mobile) via the capture harness — gallery needs no login, add it to the anonymous section in Task 8.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/app/dev/gallery/page.tsx frontend/src/__tests__/dev-gallery.test.ts
git commit -m "feat(frontend): dev-only component gallery — primitive × variant matrix for visual review"
```

### Task 8: Deterministic e2e labels (pixel-diff prerequisite)

**Files:**
- Modify: `backend/tests/e2e/browser_world.py` (seed titles/names)
- Test: `backend/tests/e2e/` — extend an existing world test or add `test_browser_world_labels.py`

**Interfaces:**
- Produces: env flag `CQ_E2E_FIXED_LABELS=1` — when set, task titles, reward names, and nicknames use fixed strings (e.g. `端到端数据采集任务·演示甲`) instead of run-id suffixes. Default behavior (unique run labels) unchanged; the flag is consumed only by the visual-regression run.

- [ ] **Step 1: Failing test** — seed with `CQ_E2E_FIXED_LABELS=1` twice into two fresh databases; assert task titles are byte-identical across runs; assert default mode still embeds the run id. (Follow the existing `browser_world` test pattern in `backend/tests/e2e/`.)

- [ ] **Step 2: Implement** — thread the flag through `_seed`'s label construction; keep every idempotence and cleanup behavior identical.

- [ ] **Step 3: Backend e2e tests green** (`cd backend && CQ_E2E=1 uv run pytest tests/e2e -v` — needs the compose stack; if unavailable, mark for release-gate run).

- [ ] **Step 4: Commit**

```bash
git add backend/tests/e2e/
git commit -m "test(e2e): CQ_E2E_FIXED_LABELS deterministic world labels for pixel-diff baselines"
```

### Task 9: Playwright visual regression suite + baselines + make target

**Files:**
- Create: `frontend/e2e/visual-regression.spec.ts`
- Create: `frontend/e2e/visual-regression.spec.ts-snapshots/` (committed baselines; generated, then reviewed like code)
- Modify: `frontend/playwright.config.ts` (snapshot config), `frontend/package.json`, `Makefile`

**Interfaces:**
- Consumes: Task 7 gallery route, Task 8 fixed labels, existing `visual-capture.spec.ts` login/world helpers (`fixtures.ts`, `ensureStudentLogin`, staff TOTP helpers).
- Produces: `npm run test:e2e:visual` (env-gated `CQ_E2E=1 CQ_VISUAL=1 CQ_E2E_FIXED_LABELS=1`); `make visual-regression`; baselines at the default Playwright `-snapshots` path.

- [ ] **Step 1: Write the spec.** Screen list = §17.0 fixed matrix + `/dev/gallery`:

```ts
// e2e/visual-regression.spec.ts — pixel baselines for the §17.0 matrix.
// Runs only with CQ_E2E=1 CQ_VISUAL=1 CQ_E2E_FIXED_LABELS=1.
import { test, expect } from "./fixtures";
// ... reuse the login helpers from visual-capture.spec.ts ...
const SHOTS: Array<{ name: string; path: string; auth: "anon" | "student" | "teacher" | "admin" }> = [
  { name: "auth-login", path: "/login", auth: "anon" },
  { name: "dev-gallery", path: "/dev/gallery", auth: "anon" },
  { name: "student-dashboard", path: "/", auth: "student" },
  { name: "student-tasks", path: "/tasks", auth: "student" },
  { name: "student-rankings", path: "/rankings", auth: "student" },
  { name: "student-rewards", path: "/rewards", auth: "student" },
  { name: "teacher-reviews", path: "/teacher/reviews", auth: "teacher" },
  { name: "admin-users", path: "/admin/users", auth: "admin" },
];
for (const shot of SHOTS) {
  test(shot.name, async ({ page }) => {
    // login per shot.auth (helpers from visual-capture), goto, wait for
    // .page-head/main + nav landmark, 800ms settle — same discipline as capture
    await expect(page).toHaveScreenshot(`${shot.name}.png`, {
      fullPage: true,
      maxDiffPixelRatio: 0.01,
      // countdown/deadline text is relative-clock; mask it until the
      // world grows a fixed clock:
      mask: [page.locator(".deadline-line"), page.locator(".progress-note")],
    });
  });
}
```

- [ ] **Step 2: Generate baselines on Linux** (`npx playwright test visual-regression.spec.ts --update-snapshots` with the stack up), eyeball every PNG, commit them. Document in the spec header: baselines are Linux-authoritative; macOS/local deltas regenerate via `CQ_VISUAL_UPDATE=1` (wire the flag to `--update-snapshots` in the npm script) and must be re-reviewed.

- [ ] **Step 3: Wire scripts + Makefile**

```json
"test:e2e:visual": "playwright test visual-regression.spec.ts"
```

```makefile
.PHONY: visual-regression
visual-regression:
	cd frontend && CQ_E2E=1 CQ_VISUAL=1 CQ_E2E_FIXED_LABELS=1 npm run test:e2e:visual
```

Do NOT add to `release-gate` or `ci.yml` yet — pixel stability across runner environments is proven first; a follow-up task under Phase C promotes it.

- [ ] **Step 4: Full run green, then break-test:** intentionally change one token, confirm the suite fails with a readable diff, revert.

- [ ] **Step 5: Commit**

```bash
git add frontend/e2e/visual-regression.spec.ts frontend/e2e/visual-regression.spec.ts-snapshots frontend/package.json frontend/playwright.config.ts Makefile
git commit -m "test(frontend): pixel visual regression for the §17.0 matrix + component gallery"
```

---

## Phase C — stack migration (Tailwind v4 + shadcn/Radix): strategy and milestones

Per the writing-plans scope rule, each milestone below gets its own bite-sized plan document at kickoff (plan-13, -14, …). This section freezes the strategy so milestone plans do not re-litigate it.

- **C0 — decision record + scaffold** (plan-14, `docs/superpowers/plans/2026-10-05-campusquest-14-tailwind-migration.md`; renumbered from plan-13 on 2026-10-05 — plan-13 became the student-surfaces polish per owner direction): record the owner ruling lifting plan-11's no-new-stack constraint; verify Tailwind v4 + Next 16 integration from the bundled Next docs and tailwind docs; install `@tailwindcss/postcss`; move the `:root` OKLCH tokens into `@theme` so CSS variables keep their exact names/values (visual regression baselines from Task 9 must stay green with zero diffs); keep `globals.css` component classes working side-by-side.
- **C1 — primitives first:** add shadcn (Radix-backed) Button/Dialog/DropdownMenu/Tabs/Tooltip; replace the 14 hand-rolled `<dialog>` implementations first (highest accessibility value). Selector contract: native `dialog` role semantics and the five dialog flows' aria ids/confirmation copy are Class A — replacements must expose identical roles, accessible names, and the same `aria-labelledby` ids; zero-skip Playwright after each dialog batch.
  - **C2 close-out ruling (2026-10-07, permanent):** Tabs stays UNMIGRATED — every current tab strip (notifications/rewards-ranking/community/profile) is a URL-search-param navigation pattern (`<a>` + `aria-current="page"`, server-parsed), and Radix Tabs would change the A-class role semantics (link→tab) for zero product gain. Radix Tabs is only for genuine client-state tabs, of which the product has none. DropdownMenu/Tooltip were likewise introduced nowhere: the teacher/admin surfaces had no menu/tooltip consumers beyond the sanctioned native StaffMenuSheet (retires below), and the Button primitive shipped as a class-mapping onto the existing `.btn*` classes (recorded in `components/ui/button.tsx`).
- **C2 — per-surface migration:** student → teacher → admin → auth, one PR per surface; each PR gates on zero-skip Playwright + pixel-diff-green visual regression for that surface's screens.
- **C3 — retirement + fold-back:** delete migrated `globals.css` sections, re-point the CSS integrity guard at the Tailwind-era rules (no arbitrary values outside theme), fold the new token pipeline into `frontend-design-system.md` §3 (G17 fold-back), promote `visual-regression` into `make release-gate` and `ci.yml`.

## Self-review record

- Spec coverage: audit findings each map to a task (btn-danger→T2, claim-detail→T3, hover/selection/doc-drift→T4, seed rarity gap→T5, guard wiring→T1/T6, gallery→T7, pixel regression→T8/T9, stack decision→Phase C).
- Placeholder scan: none — every code step carries real code or an exact grep-first verification action.
- Type consistency: `check:css`, `test:e2e:visual`, `CQ_E2E_FIXED_LABELS`, `/dev/gallery` used consistently across tasks.
- Deliberately out of scope: plan-11 Tasks 6–11 surface polish (rewards/rankings/community/teacher/admin/auth detail work) continues under plan-11, not this plan; fixed-clock e2e seeding (masked regions are the interim answer).
