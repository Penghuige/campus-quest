# 双创 R2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 所有已公开成果内容更新重新审核，待审、退回、撤回保留旧通过版本，批准才替换。

**Architecture:** 复用不可变 AchievementRevision 与多轮 AchievementReviewCase。首次通过状态不回退，当前事项独立表示更新状态；同事务写公开指针、通知和审计。公开投影只读取通过版本及白名单字段。

**Tech Stack:** FastAPI、SQLAlchemy async、PostgreSQL、项目 Clock、Next.js/React、Playwright；不新增依赖。

**Spec:** `docs/superpowers/specs/2026-10-10-innovation-update-review-design.md`

## Global Constraints

- 沿用 codex/innovation-platform；基线 3431b20fb7b8c6ffd7c87276516e0598a3929b16。
- 不重置开发数据或共享测试库、不修改 main、不合并，PR 保持 Draft。
- 本轮只推进 R2，招募和统一认证保持已确认后续安排。
- API 面变动同时提交 OpenAPI 快照和生成类型，双树门禁对准当前改动。
- 真实 PostgreSQL 验证锁、事务和不变量；使用独占测试库。
- 主实施保持当前模型；最后独立评审切换模型前告知用户。

## Review Focus

1. 首次通过状态与最新事项状态不一致时，负责人仍准确知道是否允许编辑和提交（Task 2 状态测试）。
2. 旧提交和决定请求在新一轮提交后重放，不能替换或关闭新版（Task 1 幂等测试）。
3. 已下架成果更新通过仍不得公开（Task 1 下架测试）。
4. 项目草稿在提交后保存，运营和公开端分别看到固定待审版、旧通过版（Task 1 快照测试）。
5. 历史免复审 UPDATE 没有审核事项，页面不得标注已复审（Task 1 投影、Task 2 文案测试）。

### Task 1: 更新复审事务和接口

**Files:** 修改 backend/app/modules/innovation/{review_service,review_operations_service,workflow_guards,review_router,review_schemas,public_achievement_service}.py；测试 backend/tests/integration/innovation/{test_achievement_workflow,test_public_achievements,test_review_races}.py，新增 test_update_review.py；更新 docs/quality/test-matrix/innovation-review.md（按现有实际矩阵路径归并）。

**Interfaces:** consumes 已保存版本与 SavedRevisionCommand；produces `submit_update(...) -> AchievementWorkflowResponse`、ReviewCaseSummary.operation、PublicAchievementResponse.latest_reviewed_at。

- [ ] 写失败测试：V1 真通过、V2 提交/退回/撤回仍公开 V1，V3 通过才替换，首次时间不变；重复提交仅一事项，待审编辑阻断，旧命令不影响新事项，下架不恢复、通知审计失败回滚。
- [ ] 执行独占 PostgreSQL focused suite，Expected: 新路由 404 或公开指针断言失败（RED）。
- [ ] 实现 submit-update 生成 UPDATE 快照与待审事项；更新决定/撤回按 operation 保留首次状态；共享编辑 guard 识别待审事项；公开时间源于对应通过记录。
- [ ] 扩展现有真实连接 race 为首次和 UPDATE 两种情境；批准/撤回两个顺序只有首个成功，公开指针匹配胜者。
- [ ] 执行创新创业集成模块，Expected: 0 failures；静态 format/lint/mypy 0；再提交后端和矩阵变更。

### Task 2: 负责人、运营、校内浏览 UI

**Files:** frontend/src/features/innovation/{AchievementReviewPanel,AchievementReviewsView,PublicAchievementsView,reviewApi,reviewPresentation}.tsx/ts 及对应单测、frontend/e2e/innovation-review.spec.ts；frontend/openapi.json 与 src/lib/api/schema.d.ts（采用实际已有路径）。

**Interfaces:** consumes Task 1 新路由和 operation/latest_reviewed_at；produces 按当前事项判断待审、退回、撤回文案及真实三账号更新复审体验。

- [ ] 写状态/公开展示失败测试：已首次通过+UPDATE SUBMITTED/RETURNED/WITHDRAWN 与历史无审核时间 UPDATE 不混淆。Expected: 缺少新状态文案/接口失败。
- [ ] 生成 API 工件，实现更新复审确认、等待旧版展示、退回原因和运营操作类型；不展示内部编号或私有资料。
- [ ] 将原即时更新 e2e 改为提交后观察者仍看旧版、运营批准后看新版，加入撤回/退回及 ACK 丢失重试情境。
- [ ] 前端 typecheck/lint/check:css/unit/coverage/build，Expected: 全部 exit 0、覆盖棘轮通过；提交界面、测试和工件。

### Task 3: 双树验证、独立评审、交付

**Files:** 质量验证记录、R2 handover、计划完成记录；产品视觉发生的基线独立提交并逐张走查。

**Interfaces:** consumes Task 1/2 完整实现；produces 最新本地 gate、真实浏览器记录、精确 HEAD 的 Draft PR/CI 回执和验收说明。

- [ ] 新鲜后端全静态/单测/PG集成/greenlet+thread coverage、API drift、矩阵检查；前端全门禁/e2e/no-skip/像素/a11y/audit（独占库，借用自有本地服务端口后恢复）。Expected: 全部 exit 0 或逐项如实记录真实外部阻塞，不能替用历史数字。
- [ ] 一次新上下文整体评审，给出范围基线至当前 HEAD、spec/plan/裁决；Important/Critical 先 RED 再修复和重新验证，不安排循环复评。
- [ ] 提交交接/验证证据，推送 feature 分支，更新现有 Draft PR，并核验 fork/PR HEAD 与当前提交相同的 CI 状态。
- [ ] 恢复本地可体验环境，给出负责人/运营/观察者验收步骤、验证结果、Git 状态和 CI 限制。

## 自检

三任务覆盖全部 spec；Task 2 消费 Task 1 的准确字段，Task 3 验证两树同一实现。现有确认授权直接执行；原免复审设计以本 R2 spec 为准。无需新增表或迁移，现有每成果唯一待审事项索引与不可变历史约束继续生效。
