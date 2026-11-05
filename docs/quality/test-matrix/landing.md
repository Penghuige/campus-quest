# Public Landing integration test matrix

> 覆盖章节：Q14视觉定稿、Q15访客/会话路由，以及2026-11-05 Landing接入设计。

Spec: docs/superpowers/specs/2026-11-05-landing-integration-design.md; Q14 visual/Q15 entry routing approved. Expectations derived before adding tests.

| # | Rule | Expected | Test | Status |
|---|---|---|---|---|
| L1 | Q15 anonymous root | SSR public Landing; /tasks still login-gated | `frontend/e2e/landing.integration.spec.ts::anonymous root is public SSR; protected routes still require login` | 已覆盖 |
| L2 | real entry | Both entry points navigate top-level /login retaining prefix | `frontend/e2e/landing.integration.spec.ts::public introduction works independently; both entries navigate the TOP login` | 已覆盖 |
| L3 | local runtime | Every declared asset returns exact bytes and correct JS MIME; missing stays404 | `frontend/e2e/landing.integration.spec.ts::every declared runtime asset is served byte-for-byte with correct JavaScript MIME` | 已覆盖 |
| L4 | session authority | stale cookie falls back public; service failure shows retry, not forged dashboard | `frontend/e2e/landing.integration.spec.ts::stale cookie resolves anonymous to Landing; session outage remains retryable` | 已覆盖 |
| L5 | failure recovery | noGL/missing PNG retain clickable real login | `frontend/e2e/landing.integration.spec.ts::no WebGL and missing stamp still expose real login recovery` | 已覆盖 |
| L6 | approved mobile | static campus framing, no horizontal overflow, axe and stable screenshot | `frontend/e2e/landing.integration.spec.ts::mobile static hero is readable and accessible` | 已覆盖 |

| L7 | Q15已登录根页 | STUDENT工作台，TEACHER/ADMIN各自落点；/about始终公开 | `frontend/e2e/landing.integration.spec.ts::STUDENT keeps its authenticated workspace; about remains public` / `frontend/e2e/landing.integration.spec.ts::TEACHER keeps its authenticated workspace; about remains public` / `frontend/e2e/landing.integration.spec.ts::ADMIN keeps its authenticated workspace; about remains public` | 已覆盖 |
| L8 | 真实桌面组合 | 树到DONE，SCROLL隐藏，最终印面能进入登录 | `frontend/e2e/landing.integration.spec.ts::desktop motion reaches DONE through the real composed document` | 已覆盖 |
| L9 | 终态键盘 | 稳定新印面与Enter顶层登录 | `frontend/e2e/landing.integration.spec.ts::desktop terminal keeps the approved stamp and keyboard login` | 已覆盖 |
| L10 | module download recovery | renderer load failure still provides pointer login outside failed ESM graph | `frontend/e2e/landing.integration.spec.ts::failed renderer module retains an independent actionable login` | 已覆盖 |
| L11 | dependency/keyboard recovery | failed Three dependency restores focus and Enter navigates top-level login | `frontend/e2e/landing.integration.spec.ts::failed renderer dependency restores keyboard login focus` | 已覆盖 |

These tests use real built Next/public resources. Session-error persona responses are explicit route controls, not a backend-authentication claim. Existing role/shell/login batteries retain their real backend checks; no credentials or database are seeded by this public-only suite. Separate config uses49356 and never borrows3000/15432. Root and /campus prefix run in CI. No legitimate skips; no new no-skip exemption.

Collection ownership: `node scripts/check-landing-collection.mjs` asserts the real shared Playwright configuration excludes this public spec and the dedicated configuration retains it. The dedicated workflow owns root and /campus executions.
