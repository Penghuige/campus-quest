# Task 评分 规约-测试对账矩阵（补充面）

> 建立日期：2026-10-25 · 域：Task 评分 §20 的 wire/API、前台展示、服务层守卫与聚合端口补充面（test-coverage-campaign 批次，agent A）
> 方法论（owner 纪律）：**预期列先于测试检索从 spec 原文推导落笔**，然后逐条定位测试；找不到测试=缺口（列出不补）；测试行为与预期不符=不匹配（上报不擅改）。
> 覆盖章节：§20
> 规约来源：`docs/superpowers/specs/2026-09-19-campusquest-design.md` §20；§29 统一错误结构、§31.7 约束交叉。
> 边界让渡（不重复建行）：§20 核心行为已由 `community.md` 建行——M1（UNIQUE(task_id,user_id)+CHECK(1..5) 的 DB 约束面）、M2（仅完成过该 Task ≥1 Claim 者可评分，含跨任务/无历史负例）、M3（评分允许修改不建多条、聚合 DTO 经端口、并发首评恰一行）。本矩阵不重建这些行，只列其未覆盖的层次与侧面。
> 接线：`backend/scripts/check_test_matrix.py`（手动跑；名称级校验）；缺口编号接续全局序（G-1/G-2 points、G-3 claims、G-4 submissions）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 4 | TR1-TR4 |
| 缺口（轻，列出不补） | 1 | G-5（TR5：TaskRating 字段面无冻结断言） |
| 不匹配 | 0 | — |
| 流程行 | 1 | TR6（让渡声明 + G13 元规则） |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| TR1 | §20 全段 × §29 | wire 全链：非完成者 PUT /tasks/{id}/rating → 403 RATING_NOT_ELIGIBLE 信封；员工角色 → 403 PERMISSION_DENIED；完成者 upsert 各留一行（改评更新原行）；任务详情的 rating 字段恰为 {average, count}——聚合分+数量是公开形状的全部，无逐人星级材料 | `tests/integration/community/test_community_api.py::test_rating_eligibility_upsert_and_real_aggregate_on_task_detail`（HTTP 级：两 403 码 + rows==[3,5] + detail rating=={average: 4.0, count: 2}；跨模块 CommunityRatingSummaryPort 接线端到端） | 已覆盖（M2/M3 为服务/DB 层，本行收 wire 与任务详情面） |
| TR2 | §20 末句（前台展示面） | 前台只渲染聚合与数量：文本"评分 X.X · N 条评价"一位小数；零评或社区模块缺席显示"暂无评分"；星级选择可操作且有确定结果（完成者回执 / 非完成者类型化文案）；upsert 客户端请求体恰 {rating: 1-5 整数} | `frontend/src/__tests__/tasks-display.test.ts::aggregate rating text with one-decimal average` + `frontend/src/__tests__/tasks-display.test.ts::no ratings yet (community module absent) reads 暂无评分` + `frontend/e2e/community.spec.ts::the aggregate renders and a star tap produces a definite outcome` + `frontend/e2e/community.spec.ts::a non-completer sees the typed RATING_NOT_ELIGIBLE copy`（skip 门控负例，豁免登记在 e2e noskip-exemptions）+ `frontend/src/__tests__/community-api.test.ts::body is the 1-5 integer; the echo carries the rater's own value` | 已覆盖 |
| TR3 | §20 值域/资格的守卫顺序面（M1/M2 的服务层补充） | 值域在服务层 typed 先拒：0/6/4.5/bool → 400 VALIDATION_ERROR 且零触库，且先于一切资格门（不泄露 user/task 存在性）；闭区间端点 1/5 可评；评分是学生面（TEACHER/ADMIN 403 零写入）；停权账号 403 ACCOUNT_NOT_ACTIVE；未知任务 404；非完成者的前台文案映射为固定句"完成任务后才能评价该任务" | `tests/integration/community/test_ratings.py::test_invalid_rating_rejected_before_any_database_touch`（0/6/4.5/bool 四参数）+ `tests/integration/community/test_ratings.py::test_invalid_rating_checked_before_the_gates` + `tests/integration/community/test_ratings.py::test_boundary_ratings_accepted`（1/5 端点）+ `tests/integration/community/test_ratings.py::test_rating_is_a_student_surface`（TEACHER/ADMIN 参数化）+ `tests/integration/community/test_ratings.py::test_rating_requires_an_active_account` + `tests/integration/community/test_ratings.py::test_rating_rejects_unknown_task` + `frontend/src/__tests__/community-view.test.ts::RATING_NOT_ELIGIBLE maps to the completer-gate copy` | 已覆盖 |
| TR4 | §20 聚合读面（M3 的端口契约补充） | 聚合读取的边界契约：零评为 None（非 0 分均值——前台"暂无评分"的端口前提）；聚合严格按 task 隔离，邻居任务的评分不泄露进来 | `tests/integration/community/test_ratings.py::test_summary_is_none_with_zero_ratings`（service 与 adapter 双断言 None）+ `tests/integration/community/test_ratings.py::test_summary_is_scoped_to_one_task`（有评任务 avg==5.0/count==1，邻接任务 None） | 已覆盖 |
| TR5 | §20 字段清单 | TaskRating 携 task_id/user_id/rating/created_at/updated_at 五字段；改评时 updated_at 前移 | upsert 单行、聚合形状、隐私面均有行为测试（M1/M3、TR1-TR4）；**字段集与 updated_at 前移无任何显式断言**——字段由模型与迁移承载，前端 stub 的回显材料（community-api.test.ts 的 created_at/updated_at）非后端断言 | **缺口 G-5（轻，列出不补）** |
| TR6 | 让渡声明 + G13（quality-gates §16） | §20 核心三行为（DB 约束、完成资格、可改聚合）由 `community.md` M1-M3 承担，本矩阵不重复建行；上述语义变更必须先改 spec | ——（流程行：M1-M3 各行即其机制） | 流程行 |

## 缺口明细（列出不补）

- **G-5**（TR5）：§20 字段清单无 model_fields 式显式冻结测试，updated_at 在改评时的前移亦无断言——可观察行为（upsert 单行、CHECK 值域、聚合形状、隐私面）均已覆盖，仅字段面本身由模型与迁移 by-construction 承载。如需防漂移可对照 identity 域 `test_user_public_excludes_private_fields` 的冻结模式补一条（`set(model_fields)` + 改评前后 updated_at 比较）。按对账纪律仅列出，不动手补。

## 不匹配明细

无。本域未发现测试行为与 spec 推导预期冲突的条目。

## 备注（边界与覆盖形态注记）

- **让渡明细**：§20 三条核心规则在 `community.md`——M1（`tests/integration/community/test_community_constraints.py` 的 UNIQUE/CHECK 直插库三测）、M2（完成资格解锁三测）、M3（改评原行、聚合 DTO、并发一列）。本矩阵四行为 M1-M3 未覆盖的层次：wire 信封与详情面（TR1）、前台渲染（TR2）、服务层守卫顺序（TR3）、聚合端口边界契约（TR4）。
- TR2 的 skip 门控负例（非完成者文案）依赖 `CQ_E2E_NON_COMPLETER_STUDENT` 种子，豁免登记按 test-artifacts manifest 属 e2e 侧既有机制，本矩阵只引用不改动。
