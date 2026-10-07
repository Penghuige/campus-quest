# points/Ledger 规约-测试对账矩阵

> 建立日期：2026-10-07 · 域：points / Ledger / rankings 投影（owner 批准的规约-测试对账矩阵首域）
> 方法论（owner 纪律）：**预期列先于测试检索从 spec 原文推导落笔**，然后逐条定位测试；找不到测试=缺口（列出不补）；测试行为与预期不符=不匹配（上报不擅改——可能是 bug 也可能是 spec 缺口，由 review 裁定）。
> 规约来源：`docs/superpowers/specs/2026-09-19-campusquest-design.md` §14 / §15(+15.1) / §17(+17.1-17.3) / §31(+31.1) / §32；`docs/quality/quality-gates.md` §16（G7/G8/G13/G15）。
> 接线：`scripts/check_test_matrix.py` 校验每行非流程状态必须有测试链接（CI 接线留 P2，当前手动跑）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 23 | E1-E15、E17、E18、E20-E24 |
| 缺口（轻，列出不补） | 2 | G-1（榜单 DTO 字段集显式冻结测试）、G-2（积分列 integer 类型冻结测试） |
| 不匹配 | 0 | — |
| 流程行（无对应测试，按定义） | 1 | E25（G13 元规则——本矩阵自身即其机制） |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| E1 | §15 首句 | 积分变更只能经 Ledger 路径；users 表无 points 直改通道 | `tests/integration/points/test_ledger_service.py::test_post_entry_projects_wallet_columns`（一切余额变动经 post_entry）+ User 模型无 points 列（wallet 为独立投影表） | 已覆盖 |
| E2 | §15 原则① | Ledger 行不可 UPDATE amount、不可 DELETE（不可变历史） | `tests/integration/points/test_points_constraints.py::test_ledger_rows_are_append_only_by_convention`（模型无 onupdate + INSERT-only 服务路径；**DB 无触发器为已裁决取舍**——测试 docstring 记录 ruling：触发器会把运行时策略放进 schema，与 submission 审计表同判） | 已覆盖（约定式+裁决注记） |
| E3 | §15 原则② + §17.2 | 修正=新增反向流水，reversal_of_id 链接原行 | `tests/integration/points/test_reward_reversal.py::test_reverse_posts_negative_entry_linked_to_untouched_original` | 已覆盖 |
| E4 | §15 原则③ | ADMIN_ADJUSTMENT 无 reason 拒绝 | `tests/integration/points/test_ledger_service.py::test_admin_adjustment_without_reason_rejected_at_database`（DB 层）+ `tests/integration/points/test_admin_adjustment.py::test_adjustment_gates_refuse_before_anything_is_written`（服务层前置拒绝） | 已覆盖 |
| E5 | §15 原则④ + §17.1 | ADMIN_ADJUSTMENT 默认 affects_ranking=false | `tests/integration/points/test_ledger_service.py::test_admin_adjustment_is_ranking_neutral_in_the_postgres_aggregate` + `tests/integration/points/test_admin_adjustment.py::test_adjustment_moves_wallet_but_never_the_boards` | 已覆盖 |
| E6 | §15.1 + §17.1 | 兑换不动 earned/历史排名（REDEMPTION：balance=true、ranking=false） | `tests/integration/points/test_ledger_service.py::test_post_entry_projects_wallet_columns`（earned 仅随正 ranking 行移动；REDEMPTION -30 不降 earned） | 已覆盖 |
| E7 | §15.1 + G7 | Ledger 是事实源；投影可从 Ledger 重建；Ledger+投影同事务 | 同事务：`tests/integration/points/test_ledger_service.py::test_wallet_projection_failure_rolls_back_ledger_insert`；可重建：`tests/integration/points/test_reward_reversal.py::test_reversal_overdraft_drives_available_negative`（sum(affects_balance)==wallet 可加和性断言） | 已覆盖（可重建为断言级；无独立 wallet 重建工具，注记见末节） |
| E8 | §15.1 | DB raw 余额保留真实负数不 clamp；用户侧 available/debt/spendable 三 clamp | DB：`test_points_constraints.py::test_wallet_available_overdraft_persists`；用户侧：`tests/unit/points/test_wallet_clamp.py`（5 例）+ `tests/integration/points/test_points_api.py::test_points_me_clamps_the_overdrawn_wallet_into_point_debt` | 已覆盖 |
| E9 | §14 步骤①-⑩ | approve 单事务十步（锁 Claim→验证→状态流转→唯一 Ledger→审计） | `tests/integration/points/test_submission_reward.py::test_approve_maps_port_call_to_ledger_source_triple_and_projects_wallet` + e2e `tests/e2e/test_concurrency_gate.py` | 已覆盖 |
| E10 | §14 唯一约束 + §31.6 | UNIQUE(source_type, source_id, ledger_type)；一 Claim 至多一条 ASSIGNMENT_REWARD（DB 约束） | `tests/integration/points/test_points_constraints.py::test_duplicate_source_triple_rejected`（直接插库触发约束） | 已覆盖 |
| E11 | §14 并发段 + G15 | 双 Teacher 并发 approve 恰发一次；另一方幂等成功/ALREADY_REVIEWED | `tests/integration/points/test_submission_reward.py::test_concurrent_reviewers_grant_reward_exactly_once` + `test_ledger_service.py::test_concurrent_duplicate_grant_posts_reward_once`（独立连接真并发） | 已覆盖 |
| E12 | §17.1 | 三榜口径=有效任务贡献（affects_ranking），非可消费余额 | `tests/integration/points/test_admin_adjustment.py::test_adjustment_moves_wallet_but_never_the_boards` + `tests/integration/points/test_submission_reward.py::test_approve_grant_enqueues_ranking_projection_and_boards_converge` | 已覆盖 |
| E13 | §17.2 | 冲销不删原行 + REVERSAL 链接 + 正常作弊双影响 | `tests/integration/points/test_reward_reversal.py::test_reverse_posts_negative_entry_linked_to_untouched_original`（配套：`tests/integration/points/test_reward_reversal.py::test_reversal_requires_reason` / `test_reversal_requires_admin_actor` / `test_reversal_target_must_be_assignment_reward` / `test_reversal_overdraft_drives_available_negative`） | 已覆盖 |
| E14 | §17.2 末段（spec 明言"此规则必须有测试"） | reversal.ranking_effective_at = 原奖励的（8月奖9月冲→修8月榜与总榜） | `tests/integration/rankings/test_rankings.py::test_reversal_repairs_original_period_only` + `test_reward_reversal.py::test_reversal_before_snapshot_reads_the_lock_time_balance` | 已覆盖 |
| E15 | §17.3 + G7/G16 | Redis 榜是投影：PG→Redis 重建方法存在；缓存更新失败不回滚已提交事务 | 重建：`tests/workers/test_ranking_rebuild.py` + e2e `tests/e2e/test_ranking_recovery.py`；不回滚：`test_submission_reward.py::test_approve_survives_ranking_dispatcher_failure_after_commit` + `test_reward_reversal.py::test_reversal_survives_ranking_dispatcher_failure_after_commit` | 已覆盖 |
| E16 | §17 展示约束 + G11 | 榜单 DTO 恰含 nickname/display_honor/score/rank；学号/手机/邮箱不可表示 | 值面：`tests/integration/rankings/test_rankings.py::test_top_orders_desc_and_enriches_display_profile`（恰好四字段值断言）；DTO by-construction 冻结（`RankingEntryResponse` extra=forbid） | 已覆盖；轻缺口 **G-1**（无 UserPublic 式 model_fields 显式冻结测试，对照 `tests/integration/identity/test_registration.py::test_user_public_excludes_private_fields` 模式） |
| E17 | §31.12 | 兑换不得使 spendable 为负（含并发） | `tests/integration/points/test_points_api.py::test_redeem_insufficient_points_answers_conflict_envelope` + `tests/integration/points/test_redemption_concurrency.py::test_concurrent_double_spend_freezes_at_most_one_reservation` + `tests/integration/points/test_redemption_concurrency.py::test_wallet_funded_exactly_the_cost_redeems` | 已覆盖 |
| E18 | §31.13 | Reward stock 并发不变负 | `tests/integration/points/test_redemption_concurrency.py::test_concurrent_last_stock_yields_exactly_one_winner` | 已覆盖 |
| E19 | §31.14 | 积分列全 integer，无浮点 | 模型/迁移以 Integer 定义并被 E10/E8 类约束测试间接使用；**无显式列类型冻结测试** | **缺口 G-2（轻，列出不补）** |
| E20 | §31.15 + §31.1 | 百分比奖励 floor(base×pct)；后端统一计算 | `tests/unit/tasks/test_deadlines.py::test_reward_points_floor_for_101`（101×80%=80 家族）+ `tests/unit/tasks/test_deadlines.py::test_reward_points_floors_below_one_to_zero`（floor(0.8)=0） | 已覆盖 |
| E21 | §32 + G8 | Redemption approve/reject/fulfill 幂等 | `tests/integration/points/test_redemption_concurrency.py::test_concurrent_double_approve_posts_one_consumption_entry` + `tests/integration/points/test_redemption_concurrency.py::test_reject_releases_points_and_stock_without_consumption_entry` + `tests/integration/points/test_redemption_concurrency.py::test_fulfill_is_idempotent_and_records_metadata` | 已覆盖 |
| E22 | §32 投影行 + G8 | 排行榜投影更新幂等（事件重放不双计） | `tests/integration/rankings/test_rankings.py::test_projection_retry_converges_to_postgres_aggregate` + `tests/integration/points/test_ledger_service.py::test_grant_assignment_reward_twice_posts_one_entry_and_one_increment` | 已覆盖 |
| E23 | §32 approve 行 | submission approve 重放幂等 | `tests/integration/points/test_ledger_service.py::test_grant_assignment_reward_twice_posts_one_entry_and_one_increment` + `tests/integration/points/test_admin_adjustment.py::test_same_operation_id_and_intent_replays_the_original_entry`（operation_id 重放族） | 已覆盖 |
| E24 | §32 幂等 key 段 | 高价值写接口幂等 key（建议级）+ 无 key 时 DB 约束兜底（MUST 级） | `tests/integration/points/test_points_api.py::test_redeem_idempotency_key_is_advisory_in_v1`（V1 advisory）；兜底= E10/E17/E18 约束族 | 已覆盖（spec 的 MUST 落在 DB 约束；key 为建议级，V1 advisory 与 spec 措辞一致） |
| E25 | G13（quality-gates §16） | 上述语义变更必须先改 spec（元规则） | ——（流程行：本矩阵的建立/更新纪律即其机制，不对应单条测试） | 流程行 |

## 缺口明细（列出不补）

- **G-1**（E16 附带）：`RankingEntryResponse` 缺 `set(model_fields)` 显式冻结测试——隐私由 by-construction + 值面断言承担，对照 identity 域 `test_user_public_excludes_private_fields` 的模式可补，但按对账纪律仅列出。
- **G-2**（E19）：积分/金额列的 integer 类型无显式冻结测试——类型由模型与迁移承载，行为测试（值域、约束）间接依赖它；如需防漂移可加 `column.type` 断言族。

## 不匹配明细

无。本域未发现测试行为与 spec 推导预期冲突的条目。

## 备注（实现取舍的裁决记录，非缺口）

- E2 的 append-only 为**约定式强制**（模型无 onupdate + INSERT-only 服务路径 + 审查），DB 层不加触发器——裁决记录于该测试 docstring（与 submission 审计表同判：触发器会把运行时策略放进 schema）。
- E7 的"投影可重建"以**断言级可加和性**（sum(ledger)==wallet）证明，未提供独立 wallet 重建端点/脚本；Redis 侧重建有完整 worker + e2e。
