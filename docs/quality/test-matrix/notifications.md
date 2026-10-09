# notifications 规约-测试对账矩阵

> 建立日期：2026-10-08 · 域：事件→投递管线 / DDL 提醒调度 / 模板系统 / 投递重试 / 超时与清理 Worker（P2 批次四域之三）
> 方法论：`campusquest-test-process` skill；**预期列先于测试检索落笔**；缺口列出不补；不匹配上报不擅改。
> 规约来源：spec §25(+25.1-25.5) / §26 / §27；quality-gates §16 G8/G9。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 14 | N1-N14 |
| 缺口 | 0 | — |
| 不匹配 | 0 | — |
| 流程行 | 1 | N15 |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| N1 | §25 | 七事件业务生成→通知投递；未知事件拒绝 | `tests/integration/notifications/test_event_notifications.py::test_unknown_event_type_rejected` + `tests/unit/notifications/test_templates.py::test_seed_templates_cover_every_event_type_and_channel`（七类型×渠道种子齐备） | 已覆盖 |
| N2 | §25 | 三渠道；站内兜底 | `tests/unit/notifications/test_channel_eligibility.py::test_critical_events_always_reach_in_app_when_account_can_receive` + `tests/workers/test_notification_delivery.py::test_in_app_delivery_uses_notification_row_as_inbox` | 已覆盖 |
| N3 | §25.1 | Task 级五项策略；默认 SMS on/EMAIL verified/IN_APP on；Teacher/Admin 渠道控制 | `tests/unit/notifications/test_deadline_scheduler.py::test_task_notify_24h_disabled_skips_the_24h_event_entirely` / `test_task_channel_list_restricts_planned_channels` + `tests/unit/notifications/test_channel_eligibility.py::test_unverified_email_is_excluded_from_planned_channels`（默认资格）+ `tests/integration/notifications/test_event_notifications.py::test_claim_created_without_task_policy_rejected` | 已覆盖 |
| N4 | §25.2 四边界 | >24h 双档；4-24h 只 4h；<4h 不补发；恰值边界 now | `tests/unit/notifications/test_deadline_scheduler.py::test_30h_left_plans_both_reminders` / `test_10h_left_plans_only_4h_reminder` / `test_3h_left_plans_neither_past_reminder` / `test_exactly_24h_left_schedules_24h_reminder_for_now` / `test_exactly_4h_left_schedules_4h_reminder_for_now` | 已覆盖 |
| N5 | §25.2 取消/重判 | 提交后取消未发提醒；校验失败回行动态重判仅未来 | `tests/integration/notifications/test_inbox_visibility.py::test_suppressed_claim_skip_never_becomes_visible` + `tests/workers/test_notification_delivery.py::test_suppressed_claim_status_skips_deadline_reminder` + `tests/integration/notifications/test_event_notifications.py::test_validation_failed_rearm_plans_only_future_deliveries` | 已覆盖 |
| N6 | §25.3 | Delivery 字段族 + UNIQUE(event_key,user,channel) | `tests/integration/notifications/test_notification_constraints.py::test_duplicate_delivery_event_user_channel_rejected` / `test_same_event_key_other_channel_or_user_allowed` / `test_delivery_defaults_and_terminal_field_roundtrip` | 已覆盖 |
| N7 | §25.3 + §32 + G8 | 重试/重投不双发 | `tests/workers/test_notification_delivery.py::test_duplicate_job_delivers_exactly_once` + `tests/integration/notifications/test_event_notifications.py::test_same_revision_required_key_twice_yields_one_delivery_set` + `test_notification_delivery.py::test_concurrent_claims_produce_single_provider_call` | 已覆盖 |
| N8 | §25.4 | 有界退避梯→FAILED；未知结果同键重试 | `tests/workers/test_notification_delivery.py::test_bounded_retry_ladder_then_terminal_failure` / `test_unknown_outcome_retries_same_row_same_key` / `test_permanent_rejection_fails_immediately` | 已覆盖 |
| N9 | §25.4 | 通知失败不回滚任务/积分；事件随域事务 | `tests/integration/notifications/test_event_notifications.py::test_record_event_rolls_back_with_the_domain_transaction` + 跨域：`tests/integration/points/test_submission_reward.py::test_approve_survives_ranking_dispatcher_failure_after_commit` 族（任务状态不因投递失败回滚） | 已覆盖 |
| N10 | §25.5 | 模板 Admin 专管；Teacher 全操作拒绝 | `tests/integration/notifications/test_template_admin.py::test_teacher_is_forbidden_on_every_operation` + `test_admin_creates_template_with_version_1_and_audit` | 已覆盖 |
| N11 | §25.5 ruling | enabled=DB override 开关（非通道开关）；注册冻结渠道快照；已建通知不可变 | `tests/integration/notifications/test_template_consumption.py::test_disabled_in_app_template_falls_back_to_seed_and_still_records` / `test_managed_sms_template_freezes_a_per_channel_snapshot` / `test_channel_snapshot_is_immutable_across_edits` / `test_created_notification_keeps_its_creation_time_snapshot` | 已覆盖 |
| N12 | §25.5 | 受限变量白名单；不执行代码（表达式探针拒绝） | `tests/unit/notifications/test_templates.py::test_unknown_variable_name_is_an_invalid_template_error` / `test_expression_probes_are_rejected_never_evaluated` / `test_variable_values_are_never_rescanned` + `tests/integration/notifications/test_template_admin.py::test_unsafe_or_invalid_template_bodies_are_typed_422` | 已覆盖 |
| N13 | §26 + G8 | 超时 worker 锁+复检+EXPIRED+释放+审计；边界竞态一果 | `tests/workers/test_expire_claims.py::test_eager_scan_expires_due_claims_end_to_end` + `tests/integration/submissions/test_reward_lock.py::test_race_validation_pass_vs_expiry_candidate_at_grace_boundary` + e2e `tests/e2e/test_concurrency_gate.py::test_in_window_submission_beats_concurrent_expiry`（互引 claims C31/S15） | 已覆盖 |
| N14 | §27 + G8 | 清理幂等；404 reconcile；四不误删 | `tests/workers/test_file_cleanup.py::test_retention_matrix_deletes_only_expired_unprotected_objects`（四不误删矩阵）/ `test_missing_object_with_present_state_reconciles_with_warning` / `test_missing_object_already_marked_deleted_is_idempotent_success` / `test_second_run_makes_no_additional_delete_calls` | 已覆盖 |
| N15 | G9 | 元规则：不可恢复事件 durable handoff | ——（流程行：N6/N7 的 UNIQUE+幂等族即其执法面） | 流程行 |

## 缺口明细

无。

## 不匹配明细

无。

## 备注

- §25.5 的 owner ruling（enabled 语义三分）由四条 template_consumption 测试逐句对应——裁决语义在测试名里可直接追认。
- N9 跨域引用 points 矩阵 E15 的"投递失败不回滚"证据族；§26 worker 互引 claims 矩阵 C31/S15，不重复列行。
