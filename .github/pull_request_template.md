<!--
Before pushing: `make pre-push` should be fully green (the pre-push hook runs it automatically).
Required CI checks ("Backend (Python 3.12)", "Frontend (Node 22)", "Pixel visual regression (Playwright)")
must pass on the exact head, and the PR must be up-to-date with main.
-->

## Summary

<!-- What changed and why. Link the spec/plan/issue. -->

## Test artifacts manifest (G19)

Walk this list before requesting review; every "yes" ships in this PR.

- [ ] New/changed behavior → spec-test matrix rows added for the touched domain (`docs/quality/test-matrix/`), predictions derived from spec text before locating tests
- [ ] New e2e spec or skip-gated tests → exemption entry in `frontend/e2e/noskip-exemptions.mjs` (gate + reason) for any test that may legitimately skip
- [ ] New stable-screen UI → pixel baseline under the pinned-font discipline; never regenerate baselines to force green (font-normalization regenerations are separate commits with a walk-through note)
- [ ] New selectors that specs depend on → B-table entry in `docs/quality/e2e-selector-contract.md`
- [ ] Coverage moved → ratchet floors updated via the documented `--update`/`coverage:update` path, with the reason stated in this PR; floors only move up — a deliberate DECREASE also needs the `[floor-adjust]` marker in the PR body (the direction gate blocks silent lowering)
- [ ] API surface changed (router/endpoint docstring edits count) → regenerated OpenAPI snapshot (`dump_openapi.py`) + generated client types (`npm run api:types:snapshot`)

## Verification

<!-- Exact gate commands run on this head and their results. -->
