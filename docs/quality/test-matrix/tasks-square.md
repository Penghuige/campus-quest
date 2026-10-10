# 任务广场 规约-测试对账矩阵

> 建立日期：2026-10-10 · 域：公开任务列表（`GET /api/v1/tasks`）排序与可见性
> 方法论：`campusquest-test-process` skill 规约对账节；模板 `docs/quality/test-matrix/points-ledger.md`。
> **纪律**：预期列先于测试检索从 spec 原文推导落笔；找不到测试=缺口（列出，不补）；测试行为≠预期=不匹配（上报，不擅改）。
> 规约来源：design spec §42（含 2026-10-10 owner 裁定的任务广场排序小注记）/§6.2/§28；本矩阵随排序 PR 建立（此前该域无矩阵文件）。
> 接线：`backend/scripts/check_test_matrix.py`（手动跑）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 8 | S1-S8（S6 为行为级覆盖，行内注记） |
| 缺口（轻，列出不补） | 0 | — |
| 不匹配 | 0 | — |
| 流程行 | 0 | — |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| S1 | §42 排序注记 | 仍有可领 Assignment（AVAILABLE > 0）的任务排在已领完（AVAILABLE = 0，含全部 OCCUPIED/COMPLETED/RETIRED 与无 Assignment）的任务之前，与发布时间无关 | `tests/integration/tasks/test_marketplace_ordering.py::test_claimable_lists_before_depleted_regardless_of_publish_time`（较早发布的可领任务先于较晚发布的已领任务） | 已覆盖 |
| S2 | §42 排序注记 | 两组内部保持发布时间新→旧 | 同 S1（已领组内 -30min 先于 -1h）+ `tests/integration/tasks/test_marketplace_ordering.py::test_page_boundaries_keep_group_order_across_pages`（交错发布时间的两组目录全序） | 已覆盖 |
| S3 | §42 排序注记 | 已领完的任务仍在列表中可见，卡片不因领完而隐藏 | 同 S1（已领完三项均在 items 中）+ `tests/integration/tasks/test_marketplace_ordering.py::test_claiming_last_unit_flips_task_behind_depleted_and_abandon_restores`（领完后仍在列表，仅位置后移） | 已覆盖 |
| S4 | §42 排序注记 + §28 offset 分页 | 分页边界不错序：各页拼接等于全序，含恰跨组边的页边界 | `tests/integration/tasks/test_marketplace_ordering.py::test_page_boundaries_keep_group_order_across_pages`（limit=2 三页拼接=全序；page1 尾=可领组末、page2 头=已领组首） | 已覆盖 |
| S5 | §42 排序注记 + §8 写路径 | 领取最后一个名额（真实 claim API 写 AVAILABLE→OCCUPIED）后任务移入已领完组；abandon 回流（→AVAILABLE）后回到可领组 | `tests/integration/tasks/test_marketplace_ordering.py::test_claiming_last_unit_flips_task_behind_depleted_and_abandon_restores` | 已覆盖 |
| S6 | §42 排序注记（排序键=查询时聚合） | 卡片展示的 assignments_available 与其排序组位出自同一语句，页内不会互相矛盾 | 同 S5（翻转后位置与 counts==0 同一响应断言）`tests/integration/tasks/test_marketplace_ordering.py::test_claiming_last_unit_flips_task_behind_depleted_and_abandon_restores`；SQL 形态无逐句断言（**行为级覆盖注记**） | 已覆盖（行为级） |
| S7 | §6.2 + §42 | 公开列表仅 PUBLISHED；DRAFT 不可见 | `tests/integration/tasks/test_task_api.py::test_task_list_pagination_and_draft_invisibility`（total 不含 DRAFT；DRAFT id 经详情路由 404） | 已覆盖 |
| S8 | §28 + 路由分页界 | offset 分页：limit 1..50 / offset ≥ 0，total 为全目录而非页 | 同 S7 + `tests/integration/tasks/test_marketplace_ordering.py::test_page_boundaries_keep_group_order_across_pages`（total==5） | 已覆盖 |
