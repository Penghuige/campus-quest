# rewards/兑换 规约-测试对账矩阵

> 建立日期：2026-10-25 · 域：RewardItem 与兑换（owner 批准的规约-测试对账矩阵批次，代理 B）
> 覆盖章节：§16
> 方法论（owner 纪律 2026-10-07）：**预期列先于测试检索从 spec 原文推导落笔**，然后逐条定位测试；找不到测试=缺口（列出不补）；测试行为与预期不符=不匹配（上报不擅改——可能是 bug 也可能是 spec 缺口，由 review 裁定）。
> 规约来源：`docs/superpowers/specs/2026-09-19-campusquest-design.md` §16（RewardItem 字段 / §16.1 Redemption 状态、学期键、申请检查、窗口、限额与库存语义 / §16.2 积分冻结与审核结算 / §16.3 并发兑换）。
> 域界让渡：§16.3 并发双花与库存 oversell、spendable 不为负（§31.12/§31.14）、approve/reject/fulfill 幂等（§32）、兑换不动 earned/ranking（§15.1/§17.1）已由 `points-ledger.md` E17/E18/E21/E6 建行覆盖，本矩阵不重复建行（见备注）；审核人授权（Admin-授权-Teacher）与审核队列是 Plan 05/08 制度面，非 §16 原文行为。
> 接线：`scripts/check_test_matrix.py` 做名称级校验（CI 接线留 P2，当前手动跑）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 11 | RW1-RW11 |
| 缺口（轻，列出不补） | 3 | G-1（建议字段 description/fulfillment_instructions 落库断言，挂 RW1）、G-2（UNDER_REVIEW 中间态转移，挂 RW3）、G-3（FULFILLED 库存永久消耗独立断言，挂 RW8） |
| 不匹配 | 0 | — |
| 流程行 | 0 | — |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| RW1 | §16 字段列表 | RewardItem 携带建议字段并受 DB 域约束：stock 可空=不限、0=售罄可列、负数拒绝；point_cost 为正；per_user_term_limit 非负；窗口不倒置；enabled / requires_manual_review / created_at 可表示（spec 典型例"平时成绩 +1""无理由请假条 x1"即测试载名） | `tests/integration/points/test_points_constraints.py::test_reward_item_domain_checks_reject_invalid`（四条 CHECK 族）+ `tests/integration/points/test_points_constraints.py::test_reward_item_unlimited_and_zero_stock_persist`（null=不限、0=售罄、requires_manual_review/created_at 落库）+ `tests/integration/points/test_points_api.py::test_rewards_list_enabled_items_with_server_side_window_verdict`（目录 DTO 字段集冻结） | 已覆盖；轻缺口 **G-1**（description / fulfillment_instructions 两个建议字段仅经 create 参数传入，无落库/展示断言） |
| RW2 | §16 末段（"全部由 Admin 配置，不能写死"） | 价格/库存/学期上限是 Admin 运行时数据：创建/更新/下架 Admin-only（Teacher/Student 403）、形状校验前置零落库零审计、每次变更同事务审计；编辑只约束未来请求——已受理兑换保留请求时点 points 快照与冻结额，下一请求才用新成本/新上限 | `tests/integration/points/test_reward_admin.py::test_admin_creates_reward_item_and_audits` + `tests/integration/points/test_reward_admin.py::test_reward_item_create_rejects_invalid_shapes` + `tests/integration/points/test_reward_admin.py::test_catalogue_administration_requires_admin` + `tests/integration/points/test_reward_admin.py::test_admin_disables_item_with_mandatory_reason_and_idempotent_replay` + `tests/integration/points/test_reward_admin.py::test_cost_and_limit_changes_bind_future_requests_only` | 已覆盖 |
| RW3 | §16.1 状态图 | Redemption 状态为封闭五成员集 REQUESTED/UNDER_REVIEW/APPROVED/FULFILLED/REJECTED：未知/拼错状态 DB CHECK 拒绝；创建无指定即初始 REQUESTED；决策把待审态推向 APPROVED/REJECTED；仅 APPROVED 可 FULFILLED（未批准发放是类型化拒绝） | `tests/integration/points/test_points_constraints.py::test_redemption_status_closed_set_rejects_unknown` + `tests/integration/points/test_points_constraints.py::test_redemption_defaults_present_and_term_key_required`（初始 REQUESTED）+ `tests/integration/points/test_redemption_concurrency.py::test_fulfill_is_idempotent_and_records_metadata`（fulfill 守卫：REQUESTED 不可发放） | 已覆盖；轻缺口 **G-2**（REQUESTED→UNDER_REVIEW 转移与"创建后直接进入 UNDER_REVIEW"无 producer 无测试，见备注） |
| RW4 | §16.1 学期键段（MUST） | 创建 Redemption 时 MUST 将当前 `CURRENT_ACADEMIC_TERM` 快照落行（term_key 非空）；per-user-term-limit 按该快照键统计；管理员切换当前 term 只影响之后新申请、不改写历史 Redemption；不可用 key（空/超长）是配置失败且零落库，绝不回退自然半年/日历猜测 | `tests/integration/points/test_redemption_concurrency.py::test_term_limit_counts_snapshot_releases_on_reject_and_switches`（快照+跨 term 独立+历史行不动）+ `tests/integration/points/test_redemption_concurrency.py::test_invalid_term_key_fails_loudly_never_calendar_fallback` + `tests/integration/points/test_term_setting_provider.py::test_redemption_snapshots_the_configured_term_once_set` + `tests/integration/points/test_term_setting_provider.py::test_existing_redemptions_keep_their_term_after_the_setting_moves` + `tests/unit/points/test_academic_term_provider.py::test_corrupt_row_fails_loudly_instead_of_seed_fallback` | 已覆盖 |
| RW5 | §16.1 申请时清单 | 申请逐项检查并类型化拒绝：enabled=false 拒绝且零落库；库存占用满答 REWARD_OUT_OF_STOCK 409 冲突包；每人学期上限达到答 REDEMPTION_LIMIT_REACHED；available_points 不足面（含恰好等值边界）让渡 points-ledger E17 | `tests/integration/points/test_redemption_concurrency.py::test_disabled_item_is_not_redeemable` + `tests/integration/points/test_redemption_concurrency.py::test_term_limit_counts_snapshot_releases_on_reject_and_switches`（限额门）+ `tests/integration/points/test_points_api.py::test_redeem_out_of_stock_answers_conflict_envelope` | 已覆盖 |
| RW6 | §16.1 窗口段 | 兑换窗口统一半开区间 `available_from <= now < available_until`：起点整点时刻可兑、终点整点时刻拒；任一端 null 该方向无界（null/null 直兑）；目录 window_open 判定由服务端按业务钟计算 | `tests/integration/points/test_redemption_concurrency.py::test_window_exact_start_accepted_exact_end_rejected` + `tests/integration/points/test_points_api.py::test_rewards_list_enabled_items_with_server_side_window_verdict`（服务端 window_open，null 窗口=开）+ `tests/integration/points/test_points_api.py::test_redeem_freezes_points_atomically`（null/null 无界直兑） | 已覆盖 |
| RW7 | §16.1 上限统计段 | 每人学期上限统计同一 reward_item_id+term_key 下仍占用资格或已批准的申请（REQUESTED/UNDER_REVIEW/APPROVED/FULFILLED）；REJECTED 不占学期限额（拒绝后同 term 可再申请）；Admin 收紧上限只拒下一请求，不动已受理行 | `tests/integration/points/test_redemption_concurrency.py::test_term_limit_counts_snapshot_releases_on_reject_and_switches`（占用/REJECTED 释放/跨 term 独立配额）+ `tests/integration/points/test_reward_admin.py::test_cost_and_limit_changes_bind_future_requests_only` | 已覆盖 |
| RW8 | §16.1 库存语义段 | 库存占用按状态派生：REQUESTED 占用最后一件使下一请求 REWARD_OUT_OF_STOCK；APPROVED 仍占用（批准后他人拿不到最后一件）；REJECTED 释放预占（释放后立即可再兑）；FULFILLED 将预占转为永久消耗 | `tests/integration/points/test_points_api.py::test_redeem_out_of_stock_answers_conflict_envelope`（REQUESTED 占用）+ `tests/integration/points/test_redemption_concurrency.py::test_approve_consumes_reservation_posts_entry_and_holds_stock`（APPROVED 占用：第二人被拒）+ `tests/integration/points/test_redemption_concurrency.py::test_reject_releases_points_and_stock_without_consumption_entry`（REJECTED 释放：同物立即可再兑） | 已覆盖；轻缺口 **G-3**（"FULFILLED 永久消耗"无行为级断言——占用计数的状态集含 FULFILLED，但无测试持有 FULFILLED 行验证最后一件仍被占用） |
| RW9 | §16.2 申请成功段 | 申请成功=原子冻结积分：spendable = ledger_balance − active_reservations；每 Redemption 恰一条 reservation（UNIQUE(redemption_id)，ACTIVE→RELEASED/CONSUMED 行内原位迁移）；冻结不动 wallet available，用户面 spendable 下降、available 待批准才动 | `tests/integration/points/test_points_api.py::test_redeem_freezes_points_atomically` + `tests/integration/points/test_points_api.py::test_points_me_reports_wallet_and_spendable`（1000−100 冻结=900）+ `tests/integration/points/test_points_constraints.py::test_second_reservation_per_redemption_rejected` + `tests/integration/points/test_redemption_concurrency.py::test_wallet_funded_exactly_the_cost_redeems`（恰好等值边界）+ `frontend/e2e/rewards-ranking.spec.ts::redeem flow: confirm dialog -> pending state -> frozen spendability`（冻结在 UI 呈现，REQUESTED/待审核态） | 已覆盖 |
| RW10 | §16.2 审核通过/拒绝段 | 审核通过：释放 reservation（CONSUMED）+ 恰一条 REWARD_REDEMPTION 负流水 + wallet 扣减 + →APPROVED（重放幂等面让渡 E21）；审核拒绝：reason 必填（空白先拒且零写入）+ 释放 reservation（RELEASED）+ 不产生消费负流水 + →REJECTED + spendable 复原 | `tests/integration/points/test_redemption_concurrency.py::test_approve_consumes_reservation_posts_entry_and_holds_stock` + `tests/integration/points/test_redemption_concurrency.py::test_reject_releases_points_and_stock_without_consumption_entry` + `tests/integration/points/test_points_api.py::test_admin_approves_then_fulfills_the_redemption` + `tests/integration/points/test_points_api.py::test_admin_reject_requires_reason_and_releases_the_freeze` | 已覆盖 |
| RW11 | §16.2 末段 | APPROVED 与 FULFILLED 分离（批准兑换与实际录入/发放可不同时间完成）：fulfill 记录 fulfilled_at/note、重放 no-op 保留原始元数据、仅 APPROVED 可发放；发放不产生第二条消费流水（由 §31.6 source-triple 唯一约束兜底，points-ledger E10 引证） | `tests/integration/points/test_redemption_concurrency.py::test_fulfill_is_idempotent_and_records_metadata` + `tests/integration/points/test_points_api.py::test_admin_approves_then_fulfills_the_redemption`（两步分离链）+ `frontend/e2e/rewards-ranking.spec.ts::catalog renders, redemption consumes through the Admin chain`（浏览器链：redeem→approve→fulfill 各自独立调用） | 已覆盖 |

## 缺口明细（列出不补）

- **G-1**（RW1 附带）：建议字段 `description` / `fulfillment_instructions` 只作为 create 参数与种子数据传入，无任何落库或展示断言；其余建议字段（stock/enabled/时间窗/上限/requires_manual_review/created_at）均有行为或约束断言承担。
- **G-2**（RW3 附带）：状态图中间态 UNDER_REVIEW 无 producer 也无测试——服务决策器接受 REQUESTED/UNDER_REVIEW 两态（`app/modules/points/redemption_service.py:716,886`），但没有任何路径把兑换置为 UNDER_REVIEW（含 spec"也可在创建后直接进入 UNDER_REVIEW"）。V1"每笔兑换人工审"的折叠记录于 `test_points_api.py` 模块 docstring；从 REQUESTED 直接决策与 spec 图（REQUESTED→UNDER_REVIEW→APPROVED）的字面关系是折叠还是偏离，留 review 裁定。
- **G-3**（RW8 附带）："FULFILLED 将预占转为永久消耗"无行为级断言——库存占用为派生计数（occupancy 查询状态集含 FULFILLED），但没有任何测试在兑换 FULFILLED 后验证最后一件仍被占用/该行仍计入占用。

## 不匹配明细

无。本域未发现测试行为与 spec 推导预期冲突的条目。

## 备注（边界让渡与实现取舍，非缺口）

- **让渡 `points-ledger.md`**（行为级重叠，不重复建行）：§16.3 并发双花（1500 分双 1000 请求恰一冻结、不出现 available_points=−500）= E17（§31.12，引 `test_concurrent_double_spend_freezes_at_most_one_reservation`；同物双请求的 wallet 行锁串行补强 `test_concurrent_same_user_same_item_requests_serialize` 亦属该证据族）；库存并发防 oversell（§16.3/§31.14，"先查 stock>0 再异步减 1"禁令与 RewardItem 行锁的可观察面）= E18；approve/reject/fulfill 幂等（§32）= E21；兑换不动 earned/ranking（§15.1/§17.1）= E6（本域 RW10 的首执语义测试同时断言 affects_ranking=false，不另建行）。
- 学生目录 DTO 不含 `requires_manual_review`：G13 方案一 dormant-config hide 裁决（V1 每笔都人工审，flag 无承诺意义），记录于 `test_points_api.py` 模块 docstring——模型字段在（RW1 证据），目录隐藏为裁决非缺口。
- 审核面 role guard（学生/无授权 Teacher 403、TOTP 门、授权-收回生命周期）与审核队列（最旧优先、REJECTED 不入队）是 Plan 05/08 制度面，测试在 `tests/integration/points/test_points_api.py` / `test_reward_admin.py`，非 §16 原文行为，未建行。
- 兑换触发的通知（`tests/integration/points/test_redemption_notifications.py`）属 notifications 域。
