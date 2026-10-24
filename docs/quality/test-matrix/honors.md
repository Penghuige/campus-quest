# honors 荣誉 规约-测试对账矩阵

> 建立日期：2026-10-25 · 域：荣誉（自动荣誉类型与授予、display honor、纪念 Honor）
> 覆盖章节：§18
> 方法论（owner 纪律 2026-10-07）：**预期列先于测试检索从 spec 原文推导落笔**，然后逐条定位测试；找不到测试=缺口（列出不补）；测试行为与预期不符=不匹配（上报不擅改）。
> 规约来源：`docs/superpowers/specs/2026-09-19-campusquest-design.md` §18。
> 域界让渡：§17 榜单"只展示 display_honor"的 DTO 字段面与隐私值面已由 `points-ledger.md` E16 建行覆盖（含 `test_top_orders_desc_and_enriches_display_profile` 与前端榜单钉），本矩阵不重复建行；§19 个人成长页"已获得荣誉"的页面行为属 §19 域（截至本矩阵建立 test-matrix/ 无 §19 矩阵，已在交付报告向 owner 注明），本域仅引 growth 的 §18 period 语义钉作补充证据。
> 接线：`scripts/check_test_matrix.py` 做名称级校验（当前手动跑）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 7 | HN1-HN4、HN6-HN8 |
| 缺口（列出不补） | 2 | G-1（周期榜荣誉自动 producer，独立行 HN5）、G-2（display_honor 用户设置面无 HTTP/UI 暴露，挂 HN6） |
| 不匹配 | 0 | — |
| 流程行 | 0 | — |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| HN1 | §18 首句 | 荣誉与积分资产分离：荣誉的创建/授予全程不写积分流水；积分类荣誉按 ranking 口径（有效任务贡献）判定，不受兑换消费影响 | `tests/integration/rankings/test_honor_grants.py::test_commemorative_honor_admin_only_idempotent_and_never_ranks`（points_ledger 行数前后钉死）+ `tests/integration/rankings/test_honor_grants.py::test_earned_points_follows_the_ranking_ledger_not_the_balance`（600 earned−100 兑换仍按 500 判，非 2000 档） | 已覆盖 |
| HN2 | §18 类型列表 | 自动荣誉类型第一版固定五种 TOTAL_COMPLETED / ON_TIME_STREAK / DAILY_RANK / MONTHLY_RANK / TOTAL_EARNED_POINTS；目录是冻结规则非数据；纪念（COMMEMORATIVE）类型不在自动目录 | `tests/unit/rankings/test_honors.py::test_fixed_catalog_is_exactly_the_spec_example_set`（五类型十定义逐档钉死 + COMMEMORATIVE 排除） | 已覆盖 |
| HN3 | §18 示例列表 | 示例阈值全部可判定：首次完成(1)/累计 10/累计 50/连续 10 按时/今日卷王(日 rank 1)/本月卷王(月 rank 1)/月度 Top 3(rank≤3)；阈值含端点、低于不授、错过的中间触发在下次评估自愈补授；仅 COMPLETED claim 计入完成数；连续按时=当前连段（一次迟到即重置，与迟到出现在前/后无关的时序语义） | `tests/unit/rankings/test_honors.py::test_total_completed_thresholds_grant_inclusively_and_self_heal` + `tests/unit/rankings/test_honors.py::test_on_time_streak_grants_at_ten_or_more` + `tests/unit/rankings/test_honors.py::test_daily_rank_grants_top_one_only` + `tests/unit/rankings/test_honors.py::test_monthly_rank_grants_top_one_and_top_three` + `tests/integration/rankings/test_honor_grants.py::test_claim_completed_grants_from_all_lifetime_facts`（一次评估补齐四荣誉+重放空）+ `tests/integration/rankings/test_honor_grants.py::test_streak_counts_backwards_until_a_late_completion` + `tests/integration/rankings/test_honor_grants.py::test_only_completed_claims_count` | 已覆盖（TOTAL_EARNED_POINTS 的 500/2000/10000 档位是 V1 ruling 非 spec 数字——spec 只固定类型与卷王/Top 3 示例；出处记录于 `tests/unit/rankings/test_honors.py` 模块 docstring） |
| HN4 | §18 "自动荣誉"（完成类触发） | 完成任务自动授荣：approve 提交后触发荣誉评估、荣誉行落触发自身的事务；评估失败绝不回滚已提交的 approve（claim COMPLETED、奖励发放照常）；丢失的触发在任意后续评估按事实自愈 | `tests/integration/submissions/test_approve_honors.py::test_approve_grants_the_first_completion_honor_after_commit` + `tests/integration/submissions/test_approve_honors.py::test_approve_survives_honor_evaluation_failure` | 已覆盖 |
| HN5 | §18 "自动荣誉"（周期榜类） | 日/月榜荣誉在周期结算后自动评估授荣（今日卷王/本月卷王/月度 Top 3 的 producer 调度链路） | ——（服务层评估有测试：`tests/integration/rankings/test_honor_grants.py::test_daily_rank_one_grants_the_period_honor` / `tests/integration/rankings/test_honor_grants.py::test_monthly_rank_top_one_and_top_three_with_period_idempotency` 均为直接调用 evaluate_honors；周期结算→自动授荣的 producer 无实现无测试，`tests/integration/submissions/test_approve_honors.py` 模块 docstring 载明属 Plan 07/08 scheduled-beat 职责） | **缺口 G-1（列出不补）** |
| HN6 | §18 "多个 Honor，只能设置一个 display_honor_id" | 用户可同时持有多个 Honor（跨类型跨周期并存）；display_honor_id 单指针：仅可指向自己拥有的荣誉（未拥有/未知 id 拒绝）、可更换、可清空；称号经用户目录解析供榜单取用 | `tests/integration/rankings/test_honor_grants.py::test_display_honor_requires_ownership_and_resolves_through_directory`（拒未拥有/未知→设→换→清空全链+目录解析）+ `tests/integration/rankings/test_honor_grants.py::test_periodic_honors_are_independent_across_periods`（一人并存 4 荣）+ `tests/integration/rankings/test_honor_grants.py::test_evaluation_for_unknown_user_is_rejected` | 已覆盖（服务层语义）；轻缺口 **G-2**（set_display_honor 无任何 HTTP 端点/前端 UI 暴露——"设置"对学生不可达，仅服务层被钉） |
| HN7 | §18 period 段 | 周期性荣誉必须带 period（如 2026-09 月度第一），而非无时间语义的永久"本月卷王"：DAILY/MONTH_RANK 无 period 不可表示、终身类型带 period 不可表示（DB CHECK）；同定义跨 period 是各自独立的 Honor 行（同月重评不双授、不同月各自可授）；前端荣誉行透传 period（含 null） | `tests/integration/rankings/test_honor_grants.py::test_period_coherence_check` + `tests/integration/rankings/test_honor_grants.py::test_periodic_honors_are_independent_across_periods` + `tests/integration/rankings/test_honor_grants.py::test_monthly_rank_top_one_and_top_three_with_period_idempotency`（同期幂等）+ `tests/integration/rankings/test_honor_grants.py::test_auto_definition_and_grant_uniqueness`（(type,name,period) 唯一+授予唯一）+ `tests/unit/rankings/test_honors.py::test_rank_definitions_are_periodic_and_lifetime_are_not` + `frontend/src/__tests__/growth-view.test.ts`（§18 荣誉行 period/null 透传钉） | 已覆盖 |
| HN8 | §18 末段 | Admin 可人工创建纪念 Honor：创建与授予 admin-only（非 Admin 拒绝）、幂等（重放返回同一授予行）、AUTO 荣誉不可手授、被授予后可作 display honor；该操作不允许自动篡改排行榜积分（全程零 ledger 行） | `tests/integration/rankings/test_honor_grants.py::test_commemorative_honor_admin_only_idempotent_and_never_ranks`（admin-only/幂等/AUTO 拒手授/可展示/ledger 行数钉死） | 已覆盖 |

## 缺口明细（列出不补）

- **G-1**（HN5）：周期榜荣誉（DAILY_RANK/MONTHLY_RANK）的自动触发 producer 不存在——spec §18 把今日/本月卷王、月度 Top 3 列为"自动荣誉类型"，当前仅服务层 `evaluate_honors` 接受 rank 事件（测试直接调用函数），周期结算→自动授荣的调度链路无实现无测试（`tests/integration/submissions/test_approve_honors.py` 模块 docstring 载明属 Plan 07/08 scheduled-beat producer 职责）。
- **G-2**（HN6 附带）：display_honor 的用户设置面缺失——`honor_service.set_display_honor` 的拥有校验/更换/清空语义有测试，但无路由暴露、无前端 UI，spec"只能设置一个 display_honor_id"中的"设置"动作对学生不可达。

## 不匹配明细

无。本域未发现测试行为与 spec 推导预期冲突的条目（积分类荣誉的 500/2000/10000 档位非 spec 数字，为记录在案的 V1 ruling，见 HN3 状态注记）。

## 备注

- 榜单展示 display_honor 的 DTO/隐私面在 `points-ledger.md` E16（含 `frontend/src/__tests__/leaderboard-view.test.ts` 与 `frontend/e2e/rewards-ranking.spec.ts` 的 board 渲染钉），本域不重复建行。
- §19 个人成长页"已获得荣誉"的页面行为属 §19 域；test-matrix/ 目录截至本矩阵建立无 §19 矩阵，本域仅引 `frontend/src/__tests__/growth-view.test.ts` 的 §18 period 语义段作补充证据，不越界建 §19 行。
