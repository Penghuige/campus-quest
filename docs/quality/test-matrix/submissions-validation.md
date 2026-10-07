# submissions 校验 规约-测试对账矩阵

> 建立日期：2026-10-07 · 域：上传流 / 校验器（CSV/XLSX/SQLite）/ Submission 状态机 / 奖励锁交互 / 校验报告（P2 批次四域之一）
> 方法论：`campusquest-test-process` skill；模板 points-ledger / claims-lifecycle。**预期列先于测试检索落笔**；缺口列出不补；不匹配上报不擅改。
> 规约来源：spec §9.3（奖励分段）+ §10 + §11(+11.1-11.5) + §12(+12.1-12.4)；§31.11、§38.3/38.4 交叉。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 25 | S1-S3、S5-S26（S4 拆半：key 生成覆盖；见 G-4） |
| 缺口（轻，列出不补） | 1 | G-4（原文件名长度限制/清理无显式断言——S4 后半） |
| 不匹配 | 0 | — |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| S1 | §9.3 分段（MUST 单测）+ §38.3 | 五档边界恰值：deadline=100%、+4h=50%、+12h=20%、grace=拒；±1ms 十二边界 | `tests/unit/tasks/test_deadlines.py::test_reward_fraction_boundary_ladder`（参数化边界梯）+ `tests/integration/submissions/test_reward_lock.py::test_first_valid_on_time_submission_locks_provisional_at_full` / `test_first_valid_at_plus_two_hours_locks_eighty_percent` | 已覆盖 |
| S2 | §10 八步流 | intent→校验→短时 URL→直传→通知→元数据验证→建 Submission→Worker 异步 | `tests/integration/submissions/test_upload_finalize.py::test_full_flow_creates_version_one_submission` + `tests/integration/submissions/test_submission_api.py`（intent/finalize API 族） | 已覆盖 |
| S3 | §10 步骤2 五项校验 | Claim 权限/状态/类型声明/大小上限/提交窗口 | `tests/unit/submissions/test_upload_policy.py::test_in_window_claimed_student_passes_all_gates` + `test_upload_policy.py::test_wrong_owner_refused` / `test_non_submittable_claim_status_refused` / `test_type_outside_task_allowed_set_refused` / `test_size_over_task_limit_refused` / `test_revision_required_claim_with_future_revision_deadline_passes`（窗口） | 已覆盖 |
| S4 | §10 安全①② | object key 服务端生成；原文件名仅显示 metadata + 长度限制与清理 | key 半：`tests/integration/submissions/test_submission_constraints.py::test_duplicate_object_key_rejected`（服务端键域）+ `tests/e2e/test_file_security.py::test_pathological_files_fail_with_bounded_typed_codes`（不可构造路径）；metadata 半：`tests/integration/submissions/test_submission_api.py`（original_filename 显示字段断言）。**长度限制/清理无显式断言** | 已覆盖（key 半）；**轻缺口 G-4** |
| S5 | §10 安全③④ | 下载短时签名 URL；越权不可读他人文件 | `tests/integration/submissions/test_submission_api.py::test_download_url_issued_only_after_authorization` + `test_submission_api.py::test_download_staff_arm_requires_confirmed_totp` | 已覆盖 |
| S6 | §10 安全⑤ | 默认 200MB、按 Task 调整 | `tests/unit/submissions/test_upload_policy.py::test_size_over_task_limit_refused`（Task 上限执法）+ `test_upload_policy.py::test_in_window_claimed_student_passes_all_gates`（默认通道）；全局默认值：`tests/unit/submissions/test_upload_service.py::test_settings_default_ttls_are_the_documented_pair`（组合默认族） | 已覆盖 |
| S7 | §10 末段 解析器独立限制 | 行/列/解压大小/CPU/内存/超时各有限制 | 三校验器各有 `tests/unit/submissions/test_csv_validator.py::test_timeout_trips_with_fake_clock` / `tests/unit/submissions/test_xlsx_validator.py::test_row_cap_aborts_with_scan_incomplete_semantics` / `tests/unit/submissions/test_sqlite_validator.py::test_too_many_columns_aborts`（行/列/超时三面 × 三格式）+ XLSX 解压比族（S20 引） | 已覆盖 |
| S8 | §11 + §31.11 | version 单调递增 Claim 内唯一（DB） | `tests/integration/submissions/test_submission_constraints.py::test_duplicate_claim_version_rejected` + `test_submission_constraints.py::test_same_version_across_claims_allowed` | 已覆盖 |
| S9 | §11.1 两段状态机 | UPLOADED→VALIDATING→VALIDATED/FAILED；VALIDATED→UNDER_REVIEW→APPROVED/REVISION_REQUIRED | `tests/integration/submissions/test_submission_constraints.py::test_validation_review_and_lock_history_rows_persist` + `test_submission_constraints.py::test_submission_closed_value_sets_reject_unknown`（闭集）+ `tests/integration/submissions/test_validation_worker.py`（机器段流转）+ `tests/integration/submissions/test_review_flow.py`（人工段流转） | 已覆盖 |
| S10 | §11.1 末 | APPROVED 后 Claim→COMPLETED | `tests/integration/points/test_submission_reward.py::test_approve_maps_port_call_to_ledger_source_triple_and_projects_wallet`（同事务状态对）+ e2e `tests/e2e/test_happy_path.py` | 已覆盖 |
| S11 | §11.2 时点排他 | tier 按 submitted_at（非 worker/审核时点）；首次机器通过锁 PROVISIONAL | `tests/integration/submissions/test_reward_lock.py::test_first_valid_at_plus_two_hours_locks_eighty_percent`（submitted_at 锚定）+ `tests/workers/test_submission_validation_job.py`（worker 时点对照面） | 已覆盖 |
| S12 | §11.2 末 | INVALIDATED 历史不可覆盖 | `tests/integration/submissions/test_submission_constraints.py::test_history_rows_are_append_only_by_convention` + `test_submission_constraints.py::test_history_action_and_lock_status_closed_sets_reject_unknown` + `tests/integration/submissions/test_review_flow.py::test_invalidated_lock_permits_new_provisional_at_new_fraction`（历史保留语义） | 已覆盖 |
| S13 | §11.4 | revision_deadline=max(grace, reviewed_at+24h)；再退回重算；档位保持 | `tests/integration/submissions/test_review_flow.py::test_late_review_sets_revision_deadline_and_preserves_reward` + `test_review_flow.py::test_review_before_deadline_keeps_grace_as_revision_deadline` + `tests/integration/submissions/test_reward_lock.py::test_later_valid_revision_does_not_lower_existing_lock` | 已覆盖 |
| S14 | §11.4 末 | 未重交→EXPIRED+Assignment 释放+不发奖励+历史保留 | `tests/workers/test_expire_claims.py::test_eager_scan_expires_due_claims_end_to_end` + `tests/integration/tasks/test_task_constraints.py::test_terminal_claims_free_both_partial_indexes`（释放）+ `tests/integration/submissions/test_submission_constraints.py::test_validation_review_and_lock_history_rows_persist`（历史保留） | 已覆盖 |
| S15 | §11.5 | 窗口内有效提交的 Claim 不因审核跨 grace 被超时释放 | `tests/integration/tasks/test_submit_expire_race.py` + `tests/integration/submissions/test_reward_lock.py::test_race_validation_pass_vs_expiry_candidate_at_grace_boundary` + `tests/workers/test_expire_claims.py::test_eager_expiry_protects_claim_with_validated_current_submission` + e2e `tests/e2e/test_concurrency_gate.py::test_in_window_submission_beats_concurrent_expiry` | 已覆盖（四证） |
| S16 | §12 DSL 支持集 | formats/selector/min-max rows/required-optional/allow_extra/types/nullable/unique/max_null_ratio | `tests/unit/submissions/test_schema.py::test_spec_12_example_parses_to_expected_structure` + `test_schema.py::test_column_rule_full_options` + `test_schema.py::test_allowed_formats_valid_subsets` | 已覆盖 |
| S17 | §12 五类型 | string/integer/number/boolean/datetime | `tests/unit/submissions/test_schema.py::test_all_five_column_types_accepted` + `tests/unit/submissions/test_csv_validator.py::test_invalid_datetime_type_error`（类型执法例） | 已覆盖 |
| S18 | §12.1 | UTF-8+BOM；非 UTF-8 拒绝或 fallback 有测试 | `tests/unit/submissions/test_csv_validator.py::test_utf8_bom_accepted` + `test_csv_validator.py::test_non_utf8_gb18030_rejected_with_clear_prompt`（明确拒绝路径）+ `test_csv_validator.py::test_mid_stream_invalid_utf8_rejected` | 已覆盖 |
| S19 | §12.1 限制 | 行/列/单元格长/解析时间；dialect 失败清晰错误 | `tests/unit/submissions/test_csv_validator.py::test_row_cap_aborts` / `test_too_many_columns_aborts` / `test_extremely_long_cell_is_row_level_error` / `test_timeout_trips_with_fake_clock` / `test_undialectable_content_rejected` + delimiter sniff 族（semicolon/tab） | 已覆盖 |
| S20 | §12.2 七防 | zip bomb/解压比/sharedStrings/空行/巨列/外部链接/公式不可信 | `tests/unit/submissions/test_xlsx_validator.py::test_suspicious_compression_ratio_rejected` / `test_total_uncompressed_cap_via_lying_directory` / `test_canonical_shared_strings_keep_their_streaming_cap` / `test_million_empty_rows_terminate_fast` / `test_too_many_columns_aborts` / `test_external_links_warn_but_never_block` / `test_formula_with_cached_value_validated_as_value` + part-cap 全族（lying/renamed 变体） | 已覆盖 |
| S21 | §12.2 sheet 语义 | 指定 sheet/第一可见/找不到失败 | `tests/unit/submissions/test_xlsx_validator.py::test_selector_reads_named_sheet_case_sensitive` / `test_first_visible_sheet_used_without_selector` / `test_target_sheet_missing_fails` | 已覆盖 |
| S22 | §12.2 末 | 公式不执行；导出防 Formula Injection | `tests/unit/submissions/test_xlsx_validator.py::test_formula_with_cached_value_validated_as_value`（缓存值按值校验）+ e2e `tests/e2e/test_file_security.py::test_formula_injected_values_render_as_literal_text`（渲染字面量） | 已覆盖 |
| S23 | §12.3 十安全项 | header/只读/query_only/禁 ext/禁 ATTACH/不执行用户 SQL/服务端语句/受限 worker/超时/不挂主库 | `tests/unit/submissions/test_sqlite_validator.py::test_fake_db_file_rejected_by_header_gate` + `test_sqlite_validator.py::test_connection_policy_blocks_every_write` + `test_sqlite_validator.py::test_authorizer_callback_policy_matrix`（SQL/ATTACH/extension 授权矩阵）+ `test_sqlite_validator.py::test_validation_never_touches_the_file`（不挂主库）+ 超时族（mid_scan/immediately） | 已覆盖 |
| S24 | §12.3 表选择 | 指定名/恰一用户表/多表失败提示/系统表不算 | `tests/unit/submissions/test_sqlite_validator.py::test_selector_picks_named_table` / `test_multiple_tables_without_selector_fail` / `test_database_without_user_tables_fails` / `test_system_tables_do_not_count` | 已覆盖 |
| S25 | §12.4 结构化报告 | row_count/detected/missing/extra/type_error_counts/null_ratios/duplicate_counts/warnings/errors/duration_ms 族 | `tests/unit/submissions/test_report_preview.py::test_report_to_json_carries_the_full_124_field_list_plus_preview` + `test_report_json_round_trip_is_lossless` + `tests/unit/submissions/test_csv_validator.py::test_duplicate_unique_url`（duplicate_counts 执法例） | 已覆盖 |
| S26 | §12.4 审核面供给 | platform+keyword/版本/submitted_at/锁档/摘要/预览/下载/历史/通过退回+备注 | `tests/integration/submissions/test_submission_api.py`（审核队列与详情 DTO 断言族，含 original_filename/版本/下载）+ `tests/unit/submissions/test_report_preview.py::test_csv_preview_capped_at_configured_row_count`（前 N 行预览）+ `tests/integration/submissions/test_review_flow.py::test_require_revision_records_notification_with_the_decision`（退回+备注） | 已覆盖 |

## 缺口明细（列出不补）

- **G-4**（S4 后半）：原文件名的**长度限制与清理**无显式测试——显示 metadata 语义与不可构造路径均有覆盖，但"超长/恶意文件名被限制清理"的独立断言未定位到。按纪律列出；补法参照 upload_policy 的 gate 模式加一条即可。

## 不匹配明细

无。

## 备注

- S15 四重证据（integration 竞态×2 + worker 保护 + e2e）是本域 §11.5 核心原则的完整落地，spec 单句规则对应四测试面。
- 与 claims 矩阵互引不重复：tier 锁档交互在 claims C20-C22（本域 S11/S13 为 submissions 视角）；§31.11 在 S8。
