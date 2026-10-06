# CampusQuest 14 Tailwind v4 + Radix Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** land the design documents' prescribed styling stack — Tailwind v4 utilities over the existing OKLCH tokens, and Radix-backed dialog primitives replacing the 15 hand-rolled `<dialog>` implementations — with zero visual regression and zero selector-contract breakage.

**Architecture:** C0 (this plan, Tasks 1-3): additive scaffold — Tailwind compiles through the documented Next 16 PostCSS path, tokens bridge via `@theme inline` so utilities resolve to the SAME CSS variables (no redefinition), preflight NOT imported (zero-diff constraint), CSS guard learns to harvest compiled utilities. C1 (this plan, Tasks 4-9): shadcn-pattern primitives in `components/ui/` + dialog migration in four surface batches, contract IDs byte-identical, e2e locator updates ship in the same commits. C2 per-surface utility migration and C3 globals.css retirement are later plans (strategy frozen in plan-12 Phase C).

**Owner rulings:** plan-11's "no new framework or UI stack" constraint was lifted for this workstream (2026-10-05 scope approval "这些都做"; 2026-10-06 "进行plan 14"). New dependencies (`tailwindcss`, `@tailwindcss/postcss`, `postcss`, Radix packages, `clsx`, `tailwind-merge`) are pre-authorized by that ruling.

**Spec:** `docs/superpowers/specs/2026-09-19-campusquest-design.md`; strategy `docs/superpowers/plans/2026-10-05-campusquest-12-visual-hardening.md` §Phase C; selector contract `docs/quality/e2e-selector-contract.md` (normative: dialog role + labelledby + focus-trap/Escape survive; the ten title ids are Class A; `dialog.dialog` may be replaced by equal-strength `getByRole("dialog")` only with the spec update in the SAME commit).

**Verified environment facts (main @ bcb848a):** Next 16.3.5 bundled docs bless the v4 path (`01-app/01-getting-started/11-css.md` L28-59: `tailwindcss @tailwindcss/postcss postcss` + `postcss.config.mjs` + `@import "tailwindcss"`); no existing PostCSS pipeline; `globals.css` sole stylesheet (layout.tsx:3); 15 product dialogs + 1 gallery showcase (inventory in Task 5-9 files lists); `.dialog` styling is a static card + `::backdrop` scrim with NO animations (globals.css:2720-2753, 3790-3794); guard's defined-set reads only globals.css.

## Global Constraints

- **Zero visual regression at every step.** C0 acceptance = `make visual-regression` 11/11 green WITHOUT regenerating any baseline. C1 dialog screens: only the gallery dialog block may drift (regenerate with review); all product shots must stay green untouched.
- Selector contract: Class A frozen (roles, accessible names, labels, the ten dialog title ids, confirmation copy, focus-trap/Escape); `dialog.dialog` → `getByRole("dialog")` locator swaps land in the SAME commit as the DOM change.
- Tokens stay OKLCH in `:root`; `@theme inline` REFERENCES `var(--…)` — it must not emit new values or redefine existing variables. `src/lib/designTokens.ts` mirror + its pinning tests (`pwa-manifest.test.ts`, `sidebar-preference.test.ts`, `tasks-display.test.ts`) stay green.
- No preflight/reset import in C0 (`@import "tailwindcss/utilities" layer(utilities);` only) — a Tailwind preflight would silently restyle the whole product and violate zero-diff.
- `npm run check:css` green after every commit; new CSS before the consolidated reduced-motion block; reduced-motion honored for any new transition.
- Read `frontend/node_modules/next/dist/docs/` (11-css.md; turbopackLocalPostcssConfig.md if PostCSS resolution misbehaves) before improvising Next integration.
- Every task: check:css + typecheck + lint + unit + build green; Playwright zero-skip for covering specs (3200/8200 port overrides; MinIO CORS already allows :3200).
- shadcn-generated code is reviewed like handwritten code (AGENTS.md); adapt generated styling to CampusQuest tokens — never accept the default shadcn theme.

---

### Task 1: Tailwind v4 scaffold (zero-diff)

**Files:**
- Modify: `frontend/package.json` (deps: `tailwindcss`, `@tailwindcss/postcss`, `postcss`)
- Create: `frontend/postcss.config.mjs`
- Modify: `frontend/src/app/globals.css` (prepend import + theme bridge)
- Test: `frontend/src/__tests__/pwa-manifest.test.ts` + full gate (must stay green untouched)

- [ ] **Step 1:** `cd frontend && npm install tailwindcss @tailwindcss/postcss postcss` (record exact installed versions in the report).
- [ ] **Step 2:** `postcss.config.mjs`:

```js
const config = { plugins: { "@tailwindcss/postcss": {} } };
export default config;
```

- [ ] **Step 3:** Prepend to globals.css (BEFORE the existing `:root`; the rest of the file is untouched):

```css
@import "tailwindcss/utilities" layer(utilities);

@theme inline {
  --color-primary: var(--primary);
  --color-primary-strong: var(--primary-strong);
  --color-primary-foreground: var(--primary-foreground);
  --color-background: var(--background);
  --color-surface-1: var(--surface-1);
  --color-surface-2: var(--surface-2);
  --color-surface-brand: var(--surface-brand);
  --color-foreground: var(--foreground);
  --color-muted-foreground: var(--muted-foreground);
  --color-border: var(--border);
  --color-border-strong: var(--border-strong);
  --color-success: var(--success);
  --color-warning: var(--warning);
  --color-danger: var(--danger);
  --color-danger-foreground: var(--danger-foreground);
  --color-rarity-normal: var(--rarity-normal);
  --color-rarity-rare: var(--rarity-rare);
  --color-rarity-epic: var(--rarity-epic);
  --color-rarity-legendary: var(--rarity-legendary);
}
```

(Adjust the list to the actual `:root` names — grep first; `@theme inline` never emits values.)

- [ ] **Step 4:** Smoke: `npm run build` green; add one throwaway utility locally (e.g. `className="flex"`) to confirm generation, REMOVE it, then the proof: `make visual-regression` 11/11 green with **zero baseline updates**; `check:css` green (no utilities used yet, so no false positives).
- [ ] **Step 5:** Commit `build(frontend): Tailwind v4 scaffold — utilities-only import, @theme inline token bridge, zero visual diff`.

### Task 2: Guard learns the compiled pipeline

**Files:**
- Modify: `frontend/scripts/check-css-integrity.mjs`
- Test: `frontend/src/__tests__/css-guard.test.ts` (new — pins the false-positive case)

**Interfaces:**
- Produces: guard v2 — defined set = globals.css classes ∪ classes from the PostCSS-compiled output (same pipeline as the app). No new runtime dependency: use the already-installed `postcss` + `@tailwindcss/postcss` programmatically.

- [ ] **Step 1:** Failing test: a tsx fixture using `className="flex items-center bg-surface-1 md:grid"` must NOT error; `className="definitely-not-a-real-class"` must error. (Test drives the guard as a subprocess against a fixture dir; `md:grid` documents the variant-syntax handling.)
- [ ] **Step 2:** Implement: in the guard, compile `src/app/globals.css` via `postcss([tailwindcss()]).process(css, { from })`, harvest `.class` tokens from `result.css` into the defined set; keep all existing checks. Handle `@utility`-less plain variants by relying on the compiled output (Tailwind generates only used candidates by scanning `src/**` — verify the scanner covers tsx; the v4 automatic content detection does).
- [ ] **Step 3:** Test green + `npm run check:css` green on the repo (no utility usage yet → identical output to before, plus the compiled harvest).
- [ ] **Step 4:** Commit `test(frontend): CSS guard harvests compiled Tailwind utilities as defined classes`.

### Task 3: C0 merge gate

- [ ] **Step 1:** Full gate fresh: check:css / typecheck / lint / unit / build; full Playwright suite (3200/8200) zero-skip; `make visual-regression` 11/11 WITHOUT baseline changes.
- [ ] **Step 2:** PR evidence: installed versions, the zero-diff proof, bundle-size delta (`next build` output before/after).
- [ ] **Step 3:** No commit (gate only); proceed to Task 4 on the same branch.

### Task 4: shadcn-pattern dialog primitive (CampusQuest-styled)

**Files:**
- Create: `frontend/src/lib/utils.ts` (`cn` — `clsx` + `tailwind-merge`; install both)
- Create: `frontend/components.json` (shadcn config: cssVariables, no RSC theme clobber)
- Create: `frontend/src/components/ui/dialog.tsx` (shadcn-pattern Radix Dialog, CampusQuest-styled)
- Modify: `frontend/package.json` (`@radix-ui/react-dialog`, `clsx`, `tailwind-merge`)
- Test: `frontend/src/__tests__/dialog-primitive.test.ts` (new)

**Interfaces:**
- Produces: `<Dialog> <DialogTrigger> <DialogContent> <DialogTitle> <DialogDescription> <DialogFooter>` shadcn-pattern API wrapping `@radix-ui/react-dialog`. Content styling replicates `.dialog` exactly: `width: min(26rem, calc(100vw - 2*var(--space-4)))` (+ a `size="wide"` variant = 40rem), `border: 1px solid var(--border)`, `border-radius: var(--radius-lg)`, `box-shadow: var(--shadow-dialog)`, `background: var(--surface-1)`, scrim `var(--overlay-scrim)`, NO open/close animation (current product has none). Radix provides role=dialog, aria-modal, focus trap, Escape, outside-click close.

- [ ] **Step 1:** `npm install @radix-ui/react-dialog clsx tailwind-merge`; write `cn` per the shadcn pattern (three lines).
- [ ] **Step 2:** shadcn init MINIMAL: run `npx shadcn@latest init` per official docs with non-interactive flags; immediately review everything it writes — it must NOT edit globals.css or tokens (revert any such edit; only `components.json` + config survive). If the CLI insists on theme injection, skip the CLI entirely and hand-write `components.json` — record which path was taken and why.
- [ ] **Step 3:** `npx shadcn@latest add dialog` — then REWRITE the generated styling to the constraints above (token references via `cn` + arbitrary-value Tailwind or plain CSS in globals.css `.cq-dialog*` classes — pick one, document the choice; the CSS-guard must stay green either way).
- [ ] **Step 4:** Unit test: renders role=dialog with aria-modal, aria-labelledby passthrough, Escape/outside-click close callbacks fire (jsdom-free — follow the repo's `tsx --test` pattern with a DOM shim if the repo has one; otherwise test composition/props mapping only, and rely on Task 5's live e2e for behavior).
- [ ] **Step 5:** Gates: check:css + typecheck + lint + unit + build; visual regression untouched (primitive unused so far).
- [ ] **Step 6:** Commit `feat(frontend): shadcn-pattern Dialog primitive styled to CampusQuest tokens`.

### Task 5: Dialog batch A — submissions + rewards (3 dialogs)

**Files:**
- Modify: `frontend/src/features/rewards/RedeemDialog.tsx` (`redeem-dialog-title`)
- Modify: `frontend/src/features/admin/SubmissionReview.tsx` (`approve-title`; dynamic `invalidate-title`/`revision-title`)
- Modify: `frontend/e2e/rewards-ranking.spec.ts`, `frontend/e2e/teacher.spec.ts` (locator swaps, SAME commit)

- [ ] **Step 1:** Migrate each dialog to the Task 4 primitive; aria-labelledby ids byte-identical (incl. the dynamic one — map state to the same two ids); confirmation copy verbatim; `dialog.dialog` locators in covering specs become `getByRole("dialog")` in the same commit; `.dialog`-specific layout classes replaced by the primitive's size variant.
- [ ] **Step 2:** Covering specs green (rewards-ranking, teacher review flow incl. 退回修改/通过并发放奖励 confirmation copy); full suite zero-skip.
- [ ] **Step 3:** Commit `refactor(frontend): dialog batch A — redeem + submission review on the Radix primitive`.

### Task 6: Dialog batch B — teacher (3 dialogs)

**Files:**
- Modify: `CreateTaskDialog.tsx` (`create-task-title`, wide), `EditTaskDialog.tsx` (`edit-task-title`, wide), `TaskLifecycleActions.tsx` (`lifecycle-confirm-title`)
- Modify: `frontend/e2e/teacher.spec.ts` (SAME commit)

- [ ] **Step 1-3:** Same discipline as Task 5. Commit `refactor(frontend): dialog batch B — teacher task dialogs on the Radix primitive`.

### Task 7: Dialog batch C — admin (8 dialogs)

**Files:**
- Modify: `RedemptionsAdmin.tsx` (×3: `redemption-approve-title`/`redemption-reject-title`/`redemption-fulfill-title`), `RewardsAdmin.tsx` (×2: `reward-form-title` wide, `reward-disable-title`), `WhitelistAdmin.tsx` (`whitelist-disable-title`), `AdminUserAccounts.tsx` (`account-status-title`), `SystemAdmin.tsx` (`setting-confirm-title`)
- Modify: `frontend/e2e/admin.spec.ts` (SAME commit)

- [ ] **Step 1-3:** Same discipline. Commit `refactor(frontend): dialog batch C — admin dialogs on the Radix primitive`.

### Task 8: Dialog batch D — community + shell (4 dialogs)

**Files:**
- Modify: `CommentThread.tsx` (举报评论, aria-label), `CommunityModeration.tsx` (`moderation-delete-title`), `RevealIdentityDialog.tsx` (`reveal-identity-title`), `components/shell/StaffMenuSheet.tsx` (menusheet — evaluate: if Radix Dialog's modality fights the sheet's non-modal usage, keep it native and document why; the contract does not list it among the five flows)
- Modify: `frontend/e2e/community.spec.ts` (SAME commit)

- [ ] **Step 1-3:** Same discipline. Commit `refactor(frontend): dialog batch D — community + shell dialogs on the Radix primitive`.

### Task 9: Gallery dialog block + C1 merge gate + fold-back

**Files:**
- Modify: `frontend/src/app/dev/gallery/page.tsx` (dialog block → primitive showcase)
- Modify: `docs/quality/frontend-design-system.md` §9/§10 + `frontend-patterns.md` §10 (primitive ownership; native-dialog guidance replaced by primitive-first)
- Baselines: `dev-gallery-linux.png` (regenerate — the ONLY intended pixel drift in C1)

- [ ] **Step 1:** Migrate the gallery's dialog showcase to the primitive; regenerate the gallery baseline and eyeball it (26rem card + scrim must match the old look; this is the visual-diff evidence for the whole C1).
- [ ] **Step 2:** Docs fold-back (G17): components/ui owns Dialog; hand-rolled `<dialog>` is now forbidden for new code — one normative paragraph each in design-system/patterns.
- [ ] **Step 3:** Full gate: check:css / typecheck / lint / unit / build; FULL Playwright zero-skip; `make visual-regression` 11/11 (only the gallery baseline regenerated); grep proof: zero `<dialog` / `showModal(` left in `src/features` + `src/components` (gallery primitive internals excepted).
- [ ] **Step 4:** Commit `refactor(frontend): gallery dialog showcase on the primitive; fold dialog ownership into design sources`.

## Self-review record

- Spec coverage: plan-12 Phase C → C0 = T1-T3, C1 primitives+dialogs = T4-T9; C2/C3 remain strategy (later plans). Selector-contract dialog obligations each map to a batch task.
- Placeholder scan: scaffold/guard steps carry exact code; migration batches are same-shape mechanical work with exact IDs per file — the repo's batch convention.
- Type consistency: primitive API defined once in T4 and consumed by T5-T9; `cn`, `components.json`, `getByRole("dialog")` used consistently.
- Deliberately out of scope: per-surface utility migration (C2), globals.css retirement (C3), preflight adoption decision (recorded as a C2-era question), non-dialog Radix primitives (Tabs/DropdownMenu/Tooltip — introduced when C2 surfaces need them).
