# 任务简化批 规约-测试对账矩阵

> 建立日期：2026-10-23 · 域：任务创建一键化（target_count/通用平台）+ 发布门禁修订 + 提交校验宽松化（spec 2026-10-23-simplification-design §2/§3）
> 方法论：`campusquest-test-process` skill；**预期列先于测试检索落笔**；缺口列出不补；不匹配上报不擅改。
> 规约来源：`docs/superpowers/specs/2026-10-23-simplification-design.md`（PR #132）；interfaces.md「Task creation & publish gate」小节。
> 域界：任务广场排序/领取配额等既有域在 tasks-square.md；本表只收本批新增/修订行为。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 8 | SB1-SB8 |
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

## 缺口明细

无。

## 不匹配明细

无。
