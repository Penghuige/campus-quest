# Claim 生命周期 规约-测试对账矩阵

> 建立日期：2026-10-07 · 域：Assignment / AssignmentClaim / 奖励锁 × 修订窗口（P2 批次，矩阵下域）
> 方法论：`campusquest-test-process` skill 规约对账节；模板 `docs/quality/test-matrix/points-ledger.md`。
> **纪律**：预期列先于测试检索从 spec 原文推导落笔；找不到测试=缺口（列出，不补）；测试行为≠预期=不匹配（上报，不擅改）。
> 规约来源：design spec §7(+7.1)/§8(+8.1-8.5)/§11.2-11.3/§38.2/§31(不变量 3-6)/§32；quality-gates §16（G13/G15 嵌入相应行）。
> 接线：`backend/scripts/check_test_matrix.py`（手动跑；CI 接线按 P1 裁定留后续）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 30 | C1-C2、C4-C31（C11/C30 为行为级覆盖，行内注记） |
| 缺口（轻，列出不补） | 1 | G-3（Claim 状态转移表无集中化 pin 测试） |
| 不匹配 | 0 | — |
| 流程行 | 1 | C32（G13/G15 元规则——本矩阵即机制） |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| C1 | §7 唯一约束 + §31.3 | UNIQUE(task_id, platform, keyword) 为 DB 约束；并发导入由 UNIQUE 兜底 | `tests/integration/tasks/test_task_constraints.py::test_duplicate_assignment_platform_keyword_rejected` + `tests/integration/tasks/test_assignment_import.py::test_concurrent_confirm_exactly_one_wins`（UNIQUE 兜底）+ `tests/integration/tasks/test_task_constraints.py::test_same_keyword_other_platform_or_task_allowed`（比较规则边界） | 已覆盖 |
| C2 | §7.1 六步流程 + 八类检测 | 预览→确认→单事务写入；逐行错误呈现；预览令牌单次；权限 | `tests/integration/tasks/test_assignment_import.py::test_preview_then_confirm_lands_rows` / `test_preview_flags_db_duplicates_and_confirms_only_valid` / `test_preview_token_single_use` / `test_confirm_unknown_token_not_found` / `test_import_permissions` / `test_concurrent_confirm_reversed_row_orders_no_deadlock` + `tests/integration/tasks/test_task_api.py::test_import_preview_reports_row_errors` | 已覆盖 |
| C3 | §8.1 末句 | Claim 状态转换由服务层集中定义 | Task 侧有 transition-table parity（`tests/unit/tasks/test_task_lifecycle.py::test_transition_table_matches_spec_exactly`），**Claim 状态无对应集中化 pin 测试**（行为面由 C20-C24/C31 全流程测试间接承担） | **缺口 G-3（轻，列出不补）** |
| C4 | §7 末段 | ABANDONED/EXPIRED 是 Claim 终态；Assignment 回 AVAILABLE；RETIRED/COMPLETED 不参与分配 | `tests/integration/tasks/test_abandon.py::test_abandoned_assignment_reallocated_but_never_back_to_abandoner`（回 AVAILABLE 且改判）+ `tests/integration/tasks/test_task_constraints.py::test_completed_assignment_not_returned_by_available_query` + `test_task_constraints.py::test_terminal_claims_free_both_partial_indexes` | 已覆盖 |
| C5 | §8.2 MUST 七查 | 领取前七查（ACTIVE/PUBLISHED/全局上限/同 Task 冲突/AVAILABLE 存在/cutoff/额外限制） | `tests/unit/tasks/test_claim_eligibility.py`（单元全查，含 PAUSED 参数）+ `tests/integration/tasks/test_claim_concurrency.py::test_eligibility_failures_map_to_business_codes` | 已覆盖 |
| C6 | §8.2 并行限制 | "需行动"上限 3；CLAIMED+REVISION_REQUIRED 计入；VALIDATING/UNDER_REVIEW 不占 | `tests/integration/tasks/test_claim_quota.py::test_student_at_quota_is_refused_a_fourth_actionable_claim` + `test_claim_quota.py::test_transition_to_under_review_frees_the_quota_slot`（审核中不占）+ `tests/integration/tasks/test_abandon.py::test_revision_required_claim_is_abandonable`（REVISION_REQUIRED 计入需行动的侧面） | 已覆盖 |
| C7 | §8.2 末段 | 同用户同 Task 同时最多一个非终态 Claim（DB 保证） | `tests/integration/tasks/test_task_constraints.py::test_duplicate_nonterminal_claim_per_user_task_rejected`（partial unique index 直接插库触发） | 已覆盖 |
| C8 | §8.2 再领取 | 完成后允许再领同 Task（无非终态时） | `tests/integration/tasks/test_task_constraints.py::test_terminal_claims_free_both_partial_indexes`（终态释放两个 partial index → 可再领） | 已覆盖 |
| C9 | §8.2 再领取 | 曾 ABANDONED/EXPIRED 某 Assignment 的用户不再分到同一 Assignment | `tests/integration/tasks/test_claim_concurrency.py::test_abandoned_or_expired_assignment_not_reassigned_to_same_user` | 已覆盖 |
| C10 | §8.2 再领取 | COMPLETED 的 Assignment 永久不回 AVAILABLE | `tests/integration/tasks/test_task_constraints.py::test_completed_assignment_not_returned_by_available_query` | 已覆盖 |
| C11 | §8.3 事务形态 | 单事务 + FOR UPDATE SKIP LOCKED；不存在事务外 UPDATE | 行为级：`tests/integration/tasks/test_claim_concurrency.py::test_fifty_students_race_ten_available_assignments`（无重复分配=锁内选行语义成立）；SQL 形态无逐句断言（**行为级覆盖注记**） | 已覆盖（行为级） |
| C12 | §8.3 末段 | 一个 Assignment 同一时刻最多一个 active Claim（DB 保证） | `tests/integration/tasks/test_task_constraints.py::test_duplicate_active_claim_per_assignment_rejected`（partial index）+ `test_claim_concurrency.py::test_fifty_students_race_ten_available_assignments` | 已覆盖 |
| C13 | §8.3 用户级锁 | 同用户领取先锁稳定用户级资源再查计数（防 COUNT+INSERT 穿透） | `tests/integration/tasks/test_claim_concurrency.py::test_user_row_lock_serializes_quota_check` + `test_claim_concurrency.py::test_quota_three_blocks_fourth_concurrent_claim` | 已覆盖 |
| C14 | §8.3 随机策略 | 用户无法选择具体 Assignment | `tests/integration/tasks/test_task_api.py::test_claim_request_cannot_choose_an_assignment`（wire 级：请求不含 assignment 选择） | 已覆盖 |
| C15 | §8.4 六错误码 | 全 4xx 非 500 | `tests/integration/tasks/test_claim_concurrency.py::test_eligibility_failures_map_to_business_codes`（5 码：ACCOUNT_NOT_ACTIVE/TASK_NOT_CLAIMABLE/CLAIM_CUTOFF_REACHED/TASK_ACTIVE_CLAIM_EXISTS/NO_ASSIGNMENT_AVAILABLE）+ `test_claim_quota.py::test_student_at_quota_is_refused_a_fourth_actionable_claim`（ASSIGNMENT_LIMIT_REACHED） | 已覆盖 |
| C16 | §8.5 日限 | 每自然日（BUSINESS_TIMEZONE）最多 2 次；上限可配置 | `tests/integration/tasks/test_abandon.py::test_third_abandon_same_business_day_is_rejected` + `test_abandon.py::test_counter_resets_at_shanghai_midnight_not_utc_or_24h` + `test_abandon.py::test_dst_short_day_window_follows_local_midnight` + `tests/integration/tasks/test_abandon_settings_composition.py`（可配置） | 已覆盖 |
| C17 | §8.5 放弃语义 | 不扣积分、写行为历史、Claim→ABANDONED、Assignment→AVAILABLE、不再分给本人 | `tests/integration/tasks/test_abandon.py::test_abandon_touches_no_points_or_reward_state`（零积分触碰 + 单条 CLAIM_ABANDONED 审计）+ `test_abandon.py::test_abandoned_assignment_reallocated_but_never_back_to_abandoner` + `test_abandon.py::test_under_review_claim_cannot_be_abandoned` / `test_non_student_account_cannot_abandon`（守卫面） | 已覆盖 |
| C18 | §8.5 并发 | 放弃日限并发原子化 | `tests/integration/tasks/test_abandon.py::test_concurrent_abandons_of_different_claims_cannot_bypass_limit` + `test_abandon.py::test_concurrent_abandons_of_same_claim_count_once` | 已覆盖 |
| C19 | §8.5 + §32 | abandon API 幂等 | `tests/integration/tasks/test_abandon.py::test_abandon_replay_returns_same_terminal_result_without_recount` | 已覆盖 |
| C20 | §11.2 | 首次有效提交建 PROVISIONAL：tier 按 submitted_at、points 按 claim snapshot | `tests/integration/submissions/test_reward_lock.py::test_first_valid_on_time_submission_locks_provisional_at_full` + `test_reward_lock.py::test_first_valid_at_plus_two_hours_locks_eighty_percent` + `tests/integration/tasks/test_claim_concurrency.py::test_claim_snapshot_survives_later_task_edits` | 已覆盖 |
| C21 | §11.2/11.3 | 普通质量 REVISION_REQUIRED 保留奖励锁 | `tests/integration/submissions/test_review_flow.py::test_late_review_sets_revision_deadline_and_preserves_reward` + `tests/integration/submissions/test_reward_lock.py::test_later_valid_revision_does_not_lower_existing_lock` | 已覆盖 |
| C22 | §11.3 | INVALIDATE_REWARD_LOCK：reason 必须+审计+取消锁+重锁；grace 后重锁取最低档 20%；不自动扣历史积分 | `tests/integration/submissions/test_review_flow.py::test_invalidate_reward_lock_reason_is_mandatory` / `test_invalidate_cancels_provisional_lock_and_audits` / `test_later_valid_submission_after_invalidation_relocks_at_own_tier` / `test_relock_in_revision_window_past_grace_clamps_to_lowest_tier`（20% 最低档）；不自动扣+错发走反向 Ledger 见 points 矩阵 E3/E13（跨域引用） | 已覆盖 |
| C23 | §8 字段 + §11 | REVISION_REQUIRED 后修订窗口内可重新提交 | `tests/integration/submissions/test_submission_api.py::test_revision_required_note_is_mandatory_and_sets_window` + `tests/workers/test_submission_validation_job.py::test_failed_validation_in_revision_window_restores_revision_required`（窗口内流转） | 已覆盖 |
| C24 | §38.2 明文（G15） | 10 AVAILABLE + 50 用户并发 → 恰 10 成功、全不同、无双 active、其余 NO_ASSIGNMENT_AVAILABLE 非 500 | `tests/integration/tasks/test_claim_concurrency.py::test_fifty_students_race_ten_available_assignments`（真实 PG 独立连接）+ e2e `tests/e2e/test_concurrency_gate.py::test_fifty_students_race_ten_assignments`（HTTP 级双证） | 已覆盖 |
| C25 | §38.2 | 同用户两并发 claim 同 Task 最多一成功 | `tests/integration/tasks/test_claim_concurrency.py::test_same_user_concurrent_claims_same_task_at_most_one` + e2e `tests/e2e/test_concurrency_gate.py::test_same_task_same_user_race_claims_at_most_once` | 已覆盖 |
| C26 | §38.2 | 已有 3 需行动 Claim → 新领取失败 | `tests/integration/tasks/test_claim_concurrency.py::test_quota_three_blocks_fourth_concurrent_claim`（并发态）+ `test_claim_quota.py::test_student_at_quota_is_refused_a_fourth_actionable_claim`（稳态） | 已覆盖 |
| C27 | §38.2 | 转 UNDER_REVIEW 后释放全局槽位 | `tests/integration/tasks/test_claim_quota.py::test_transition_to_under_review_frees_the_quota_slot` | 已覆盖 |
| C28 | §38.2 | Task PAUSED → 新领取失败、已有 Claim 不变 | `tests/unit/tasks/test_claim_eligibility.py`（PAUSED 参数化拒绝）+ `tests/unit/tasks/test_task_lifecycle.py::test_pause_and_close_keep_existing_claims` | 已覆盖 |
| C29 | §38.2 + §9.1 | FIXED 距 DDL <4h 拒绝；恰好 cutoff 边界仍可领 | `tests/integration/tasks/test_claim_quota.py::test_fixed_task_past_cutoff_refuses_claims` + `test_claim_quota.py::test_fixed_task_exactly_at_cutoff_still_claims`（边界） | 已覆盖 |
| C30 | §38.2 | RELATIVE 不因全局时间接近某日期错误拒绝 | 行为级：五十人竞速即 RELATIVE 任务在冻结时钟成功领取（`tests/integration/tasks/test_claim_concurrency.py::test_fifty_students_race_ten_available_assignments`，§9.2 claimed_at 锚定注释）；`tests/unit/tasks/test_deadlines.py::test_relative_mode_uses_claimed_at_plus_duration`（口径单元）。无专门的"近日期误拒"边界用例（**行为级覆盖注记**） | 已覆盖（行为级） |
| C31 | §32 Worker 行 | Claim 超时 Worker 幂等：重复扫描/重投不双计 | `tests/workers/test_expire_claims.py::test_eager_scan_expires_due_claims_end_to_end`（second scan discovers nothing + redelivered per-ID job 幂等）+ `tests/integration/tasks/test_submit_expire_race.py`（提交×过期竞态）+ `tests/integration/submissions/test_reward_lock.py::test_race_expiry_first_then_reward_lock_noops_idempotently` | 已覆盖 |
| C32 | G13/G15（quality-gates） | 元规则：语义变更先修 spec；并发不变量真 PG——本矩阵即机制 | ——（流程行） | 流程行 |

## 缺口明细（列出不补）

- **G-3**（C3）：Claim 状态机无 transition-table parity 测试——Task 侧已有该模式（`test_task_lifecycle.py::test_transition_table_matches_spec_exactly` 把 spec 转移表逐对 pin 住），Claim 侧（CLAIMED→VALIDATING→UNDER_REVIEW→REVISION_REQUIRED→COMPLETED/ABANDONED/EXPIRED 七态）没有等价集中化断言；当前由全流程行为测试间接承担。按纪律列出，不动手补。

## 不匹配明细

无。本域未发现测试行为与 spec 推导预期冲突的条目。

## 备注（覆盖形态注记，非缺口）

- **C11 行为级**：SKIP LOCKED 的 SQL 形态无逐句断言（实现细节），其产品语义（不重复分配、并发安全）由 §38.2 明文并发测试证明。
- **C30 行为级**："近日期不误拒"由 RELATIVE 任务在竞速测试中成功领取正面证明；专门的边界用例（全局时间贴近某 FIXED 日期时 RELATIVE 不受影响）未单列。
- **跨域引用**：C22 的"错发不自动扣、走反向 Ledger"落在 points 矩阵（E3/E13）；§31.6（Claim 奖励不重复发）同样在 points 矩阵 E10——本矩阵不重复列行，两文件交叉引用。
