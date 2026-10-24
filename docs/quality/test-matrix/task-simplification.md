# 任务简化批 规约-测试对账矩阵

> 建立日期：2026-10-23 · 域：任务创建一键化（target_count/通用平台）+ 发布门禁修订 + 提交校验宽松化（spec 2026-10-23-simplification-design §2/§3）
> 覆盖章节：docs/superpowers/specs/2026-10-23-simplification-design.md#§1 · §2 · §3 · §4
> 方法论：`campusquest-test-process` skill；**预期列先于测试检索落笔**；缺口列出不补；不匹配上报不擅改。
> 规约来源：`docs/superpowers/specs/2026-10-23-simplification-design.md`（PR #132）；interfaces.md「Task creation & publish gate」小节。
> 域界：任务广场排序/领取配额等既有域在 tasks-square.md；本表只收本批新增/修订行为。
> 分工：SB1-SB8 为后端半（`simplification-backend` 分支，随后端 PR 复核）；SB9-SB14 为前端半（`feat/simplification-p1p2`）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 14 | SB1-SB14（后端 8 + 前端 6） |
| 缺口 | 0 | — |
| 不匹配 | 0 | — |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| SB1 | §3.2 | `target_count` 提供时**同事务**创建 N 个任务单元：platform=generic、keyword=单元1..N、availability=AVAILABLE；创建响应 201 即全部落库 | `tests/integration/tasks/test_task_api.py::test_create_with_target_count_creates_generic_units` | 已覆盖 |
| SB2 | §3.2 | `target_count` 未提供=行为不变（单元仍走导入）——创建后该任务零单元 | `tests/integration/tasks/test_task_api.py::test_create_without_target_count_leaves_units_to_import` | 已覆盖 |
| SB3 | §3.2 | `target_count` 界 1..200：0/201/-1 → 400 VALIDATION_ERROR | `tests/integration/tasks/test_task_api.py::test_create_target_count_is_bounded`（3 参数） | 已覆盖 |
| SB4 | §2.2 | DRAFT→PUBLISHED 零任务单元→409 `TASK_UNITS_REQUIRED`，消息含「完成人数」 | `tests/integration/tasks/test_task_api.py::test_first_publish_requires_a_task_unit` | 已覆盖 |
| SB5 | §2.2 | PAUSED→PUBLISHED 不拦（首发布专属门）——暂停态单元被移除仍可恢复发布 | `tests/integration/tasks/test_task_api.py::test_resume_from_paused_skips_the_unit_gate` | 已覆盖 |
| SB6 | §2.1/§2.2 | 结构化任务无 schema 也可创建+发布+被学生领取（旧 claim 侧 schema-version 门已移除） | `tests/integration/tasks/test_task_api.py::test_schema_less_structured_task_publishes_and_is_claimable` | 已覆盖 |
| SB7 | §2.1 | 宽松校验：可解析+有表头+≥1 数据行→通过（无列/类型/唯一性检查）；失败首行给下一步动作文案（表头-only→「至少一行数据」） | `tests/integration/submissions/test_validation_worker.py::test_schema_less_task_validates_leniently` / `::test_schema_less_failure_leads_with_the_next_step` | 已覆盖 |
| SB8 | §3.3 | 平台注册表种入 generic/通用/1000（迁移 0033 幂等）；SEED_PLATFORMS 常量与 DB 不漂移 | `tests/integration/tasks/test_platform_registry.py::test_registry_seeds_constant_matches_spec`（集含 generic）+ SB1 内 `db_session.get(Platform, "generic")` 断言 display_name=通用/sort_order=1000 | 已覆盖 |
| SB9 | §3.1/§3.2 前端 | 一键创建主表单只问业务四问（标题/描述/需要多少人完成 1-200 必填/领取后天数默认 7）；提交体携带 `target_count`；创建后详情统计显示可领取单元与总单元=3 | `frontend/src/__tests__/teacher-view.test.ts`（simplification batch：defaults/targetCount band/body target_count/days helpers）+ `frontend/e2e/teacher.spec.ts`（一键创建→详情统计 可领取单元/总单元=3） | 已覆盖（前端半） |
| SB10 | §2.1 前端 | 高级折叠默认收起且带工作默认（10 分/NORMAL/CSV/10MB/60 分钟/通知关闭）；schema 起始为空，「使用模板」按所选文件类型填起点（不再自动预填，defect #22 退役）；留空创建合法，且 OFF 通知策略确实到达服务端 | `frontend/src/__tests__/teacher-view.test.ts`（EMPTY_TASK_FORM defaults、通知默认 OFF、空 schema 创建合法、模板钉）+ `frontend/e2e/teacher.spec.ts`（高级展开→空 schema→使用模板；创建后 GET 详情钉 notify_24h=false/notify_4h=false/channels=[]） | 已覆盖（前端半） |
| SB11 | §2.2 前端 | 零单元草稿的发布按钮在调用前即禁用并显示「请先设置完成人数（任务单元）再发布」（统计未落地/null 不拦，服务端仍是裁决）；导入成功后同一按钮解禁 | `frontend/src/__tests__/teacher-view.test.ts`（publishBlockedReason 五态钉）+ `frontend/e2e/teacher.spec.ts`（API 造零单元草稿→按钮 disabled+提示→导入→enabled→发布） | 已覆盖（前端半） |
| SB12 | §2.2 错误映射 | 后端 `TASK_UNITS_REQUIRED` 到达时映射同一友好文案（共享常量；错误码入前端注册表） | `frontend/src/__tests__/teacher-view.test.ts`（错误映射钉，文案=PUBLISH_UNITS_REQUIRED_HINT）+ `frontend/src/lib/errors.ts` 注册表 | 已覆盖（前端半） |
| SB13 | §2.3 前端 | 严格族缺列 finding 显示「缺少列 <name>，可下载本任务模板」；宽松族首行动作文案由后端给出、前端原样渲染；未知码只显示服务端消息不空白 | `frontend/src/__tests__/validation-view.test.ts`（missing-column 文案钉 + 未知码回退） | 已覆盖（前端半） |
| SB14 | §3.2 通知通道 + 主 spec §25.1 | 通知策略在高级折叠可改；发布后编辑面仍可改（PATCH 只带差异：notify_24h/notify_4h/notification_channels）；开启提醒却零通道时客户端拦并提示「至少选择一个通知通道」 | `frontend/src/__tests__/teacher-view.test.ts`（round-trip/发布后编辑/零通道 band 三钉） | 已覆盖（前端半） |

## 缺口明细

无。

## 不匹配明细

无。

## 注记

- §2.3 宽松族失败文案（「请确认文件有表头并至少一行数据」）由后端报告 `message` 承载（SB7），前端 `findingItemView` 对非缺列码原样渲染——前端无独立文案转换，属 SB7 的下游显示。
- §3.2「自定义单元导入」由任务详情既有的导入面板（preview→confirm）承载；创建弹窗内不做导入（任务尚未创建，无 id 可挂）。
