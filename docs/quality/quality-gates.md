# CampusQuest Quality Gates

> Every implementation Agent and reviewer uses the relevant sections before marking work complete.

## 1. Universal gate

A change is not ready if any applicable item fails.

### Specification

- The change implements the approved spec and plan behavior.
- No product semantic was invented silently.
- No unrelated feature or refactor entered the diff.
- Stable public API and error names match project contracts.

### Tests

- New behavior has tests.
- A bug fix has a regression test that failed before the fix.
- Focused tests pass.
- Relevant module or integration gate passes.
- High-risk concurrency and idempotency behavior is tested on real PostgreSQL where applicable.

### Code quality

- Formatter passes.
- Linter passes.
- Type check passes for touched project code.
- No secrets, debug statements, or temporary files.
- No unresolved placeholder markers for required behavior.
- No broad ignored errors added merely to silence tooling.

### Reviewability

- Names explain intent.
- Non-obvious locking and transaction decisions have a short WHY comment or a test documenting them.
- Generated code was reviewed.
- Diff is small enough to review; otherwise split it.

## 2. Frontend visual gate

Review the changed flow at:

- narrow viewport;
- desktop or wide viewport;
- normal state;
- loading;
- empty where applicable;
- error;
- permission denied where applicable;
- long content.

Check:

- visual hierarchy has one obvious primary action;
- page does not become a wall of equal cards;
- spacing follows shared rhythm;
- tokens are used instead of arbitrary repeated values;
- rarity colors remain accents;
- destructive actions are visually distinct and confirmed;
- dense tables remain scan-friendly;
- mobile actions remain usable;
- status text is understandable without color.

For a new core page, capture screenshot evidence in the review or PR workflow when tooling supports it.

## 3. Frontend accessibility gate

Keyboard-only pass:

- reach all interactive controls;
- visible focus;
- logical focus order;
- dialog open and close restores focus appropriately;
- menus and tabs follow primitive behavior;
- no keyboard trap.

Content pass:

- icon-only buttons have names;
- form labels and errors are associated;
- color is not the sole state indicator;
- error and success updates are announced when focus does not naturally convey them;
- reduced motion is respected;
- touch targets are comfortably usable;
- contrast is acceptable for body, muted, and status text.

Prefer automated accessibility tests for stable screens, but automated tests do not replace a short keyboard review.

## 4. Frontend privacy gate

Inspect both API response shape and rendered DOM.

Student and public pages MUST NOT unnecessarily expose:

- student number;
- phone;
- email;
- raw object-storage key;
- private internal identifier;
- anonymous comment identity.

Do not accept hidden-by-CSS as privacy.

Anonymous Admin reveal remains an explicit action.

## 5. Frontend performance gate

Before optimizing, check high-impact issues:

- avoidable sequential data fetching;
- oversized Client Component boundary;
- accidentally bundled heavy library;
- repeated server requests for identical data;
- unbounded large table or list rendering;
- unstable provider or state causing broad rerenders.

Do not block a correct feature on speculative micro-optimization.

For a large new page, use the Vercel React best-practices skill or reference during review.

## 6. Backend architecture gate

Check dependency direction:

router -> service/use case -> repository/query/port -> adapter/database

Reject if:

- router directly performs a multi-table business transaction;
- worker contains alternate domain rules;
- feature reaches into another domain's ORM internals instead of a contract or port where isolation is required;
- one generic manager or service accumulates unrelated behavior;
- response schema exposes persistence or private fields accidentally.

## 7. Backend correctness gate

For each state-changing use case, identify:

- invariant;
- transaction boundary;
- rows or resources locked;
- database constraint fallback;
- expected conflict error;
- retry and idempotency behavior.

If these cannot be stated clearly, the code is not ready.

## 8. Database gate

For schema changes:

- Alembic migration exists;
- clean upgrade to head succeeds;
- relevant downgrade strategy is understood;
- constraint and index names are explicit;
- new hot query has appropriate indexes;
- migration does not depend on network or runtime service code.

For concurrency:

- use real PostgreSQL;
- test two or more independent transactions;
- repeat race-prone tests enough to expose flakiness.

## 9. Worker and external-system gate

For each job:

- repeated same job is safe;
- permanent and temporary provider failures are distinguished;
- retries are bounded;
- unknown external outcome uses a provider idempotency key where possible;
- job does not create duplicate business side effects;
- job calls domain service rather than editing business tables ad hoc.

## 10. Security gate

Authentication and authorization:

- state-changing route checks active account;
- resource ownership and permission are checked server-side;
- Teacher access is Task-scoped;
- Admin-sensitive action has required reason and audit where specified;
- CSRF protections remain intact for cookie-authenticated mutations.

Secrets and logging:

- no password, OTP, token, TOTP secret, or recovery code in logs or errors;
- contact information is masked where unnecessary;
- object URLs are authorized before generation.

Files:

- declared extension is not trusted;
- parser limits are enforced;
- SQLite remains read-only and query-only;
- XLSX and ZIP limits are checked;
- file content does not enter application logs.

## 11. Points and reward gate

Any change touching points or rewards must prove:

- original Ledger rows are immutable;
- idempotent source key prevents duplicate assignment reward;
- wallet and reservation update is transactional;
- spendable points never go negative;
- limited stock never oversells;
- normal redemption does not reduce historical ranking contribution;
- reversal corrects the intended historical period;
- retry does not double-update Redis ranking.

## 12. Task, Claim, and Submission gate

Any change touching these states must prove:

- Assignment is not double-allocated;
- same user and Task active rule is preserved;
- three-actionable-Claim quota is preserved;
- reward deadline boundary exactness is preserved;
- valid submission cannot race into erroneous expiry;
- Teacher review latency does not lower reward;
- revision-window semantics are preserved;
- history is not overwritten.

## 13. Community gate

- anonymous DTO cannot leak identity;
- parent belongs to the same Task;
- edit history is retained;
- soft-delete preserves child context;
- vote and reaction uniqueness is preserved;
- rate limit prevents basic spam;
- task rating requires a completed Claim;
- explicit Admin identity reveal is audited.

## 14. Completion evidence

An Agent's final implementation report should include:

- files or area changed;
- tests added;
- exact verification commands run;
- pass or exit result;
- screenshots or visual notes for frontend changes where relevant;
- known limitations that are explicitly outside scope.

"Looks good" is not verification evidence.

## 15. Release gate

V1 release is governed by the dedicated E2E hardening plan and eventual command:

~~~bash
make release-gate
~~~

A fresh zero-failure run is required after the final release-gate fix.

Until that command exists, use the strongest currently implemented subset rather than pretending the future gate has run.

## 16. Engineering Golden Rules（PR #2 起，长期 merge gate）

本节来自 PR #2 Integration Hardening 审查（owner 批准），对所有实现 Agent 与 reviewer 生效。G1/G2/G3/G17/G18 是重点 merge gate：违反即阻塞合并。

- **G1 生产组合是功能的一部分。** 只有 Service/Domain + Fake 测试通过不算完成；真实 adapter、composition root、worker wiring、scheduler wiring 都属于该功能。No production binding = not done。
- **G2 Fake 通过 ≠ 可部署。** Fake 证明领域规则；至少一条真实依赖 smoke test 证明系统能跑。CI 优先覆盖 PostgreSQL / Redis / MinIO / Celery 注册与组合。
- **G3 占位符不得进入合并。** 生产路径出现 `NotImplementedError`、`Placeholder*`、"merge later"、"Plan X will wire this"、silent no-op adapter 默认阻塞合并。允许接口 seam，但生产路径必须有真实实现或明确 fail-fast。
- **G4 外部副作用不得假成功。** 真成功才标成功；明确失败记失败；unknown outcome 必须用幂等 key/reconciliation；logging-only adapter 不得在 production 被当成成功 provider。
- **G5 生产 fail closed。** 安全、权限、provider、secret、audit 等生产依赖缺失时启动失败或请求失败，不得降级为"不做任何事但返回成功"。
- **G6 一个 event loop，一个安全的 async DB 生命周期。** 不允许 process-wide pooled AsyncEngine/asyncpg connection 跨多个 `asyncio.run()` event loop 被隐式复用；worker 统一走项目级 worker session factory，并用测试固定。
- **G7 PostgreSQL 是事实源。** Ledger/Claim/Submission/权限/审核状态以 PG 为准；Redis 排行榜/缓存必须可重建；Object Storage 不拥有业务状态；不得为修 Redis/存储状态篡改业务事实。
- **G8 at-least-once 需要业务幂等。** Celery/HTTP retry/webhook/finalize 都按可能多次执行设计；幂等靠稳定业务 key / DB constraint / 状态机，不靠"queue 只投一次"。
- **G9 跨模块副作用需要 durable handoff。** 纯投影允许 best-effort + rebuild；不可由事实源自动恢复的事件（通知、审计）必须用 durable outbox / persisted delivery intent 或等价机制。
- **G10 RBAC = 角色 + 所有权/scope + 账户状态。** "是 Teacher"≠"可操作所有 Teacher 资源"；scope 无法确定时默认拒绝。
- **G11 隐私由 DTO 形状强制，不是前端隐藏。** 敏感字段不进入不需要它的 DTO；serializer 最小披露；de-anonymization 走独立高风险端点。
- **G12 敏感读取是可审计行为。** 匿名揭示/PII 查看/管理员导出/敏感后台查询必须 durable 审计，记录 actor/target/reason/timestamp。
- **G13 Agent 不得静默改变产品语义。** points/reward、deadline/grace/revision、anonymity/privacy、RBAC、ranking semantics、institutional reward policy 的变化必须先更新设计/spec 并显式标记 owner veto；Agent 只选技术实现。
- **G14 时间边界是 API。** server-authoritative、timezone-aware、明确 </<=、exactly-at-boundary 测试；worker delay 不得改变学生应得结果。
- **G15 并发不变量需要真实 PostgreSQL 测试。** quota/stock/claim allocation/reward issuance/redemption/version allocation/abandon limits/review races 必须用独立连接的并发 integration test；mock 不算证明。
- **G16 每个派生缓存/投影需要重建故事。** 回答不了"Redis/worker/cache 全丢后从什么事实重建"的状态不应只存在于 projection。
- **G17 Merge-carry 债务不得在合并中幸存。** 集成 PR 必须把 carry 当 checklist 全部关闭，而不是把 carry 文档合进 main 当未来承诺。
- **G18 CI 必须测试组合边界。** 保持至少一组 production-like composition smoke tests，不 override 核心 provider。CI 绿 ≈ 领域规则正确 + 真实 wiring 可启动并跑通关键链路。
