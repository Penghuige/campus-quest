# identity RBAC 规约-测试对账矩阵

> 建立日期：2026-10-08 · 域：角色权限 / 学号手机邮箱昵称规则 / 密码与会话 / Staff 2FA / 账号状态治理（P2 批次四域之四）
> 方法论：`campusquest-test-process` skill；**预期列先于测试检索落笔**；缺口列出不补；不匹配上报不擅改。
> 规约来源：spec §4(4.1-4.3) / §5(5.1-5.8) / §21.4；quality-gates §16 G10/G12。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 13 | I1-I13 |
| 缺口 | 0 | — |
| 不匹配 | 0 | — |
| 流程行 | 1 | I14 |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| I1 | §4.1 不得清单 | 学生不可见他人学号/手机/邮箱/匿名真身；不可改奖励/DDL/Claim | `tests/integration/identity/test_registration.py::test_user_public_excludes_private_fields`（DTO 冻结）+ e2e `tests/e2e/test_privacy_rbac.py::test_public_and_student_surfaces_exclude_sensitive_fields`；不可改状态：`tests/integration/identity/test_account_status.py::test_student_denied_admin_only_operation` / `test_student_denied_staff_operation` + e2e `tests/e2e/test_privacy_rbac.py::test_direct_route_rbac_matrix` | 已覆盖 |
| I2 | §4.2 + G10 | Teacher 资源归属：own Task 执法；review 权限≠全 Task | `tests/integration/tasks/test_task_api.py::test_teacher_cannot_edit_another_teachers_task` / `test_teacher_task_reads_denied_for_outsiders` + `tests/integration/submissions/test_submission_api.py`（outsider 管理面拒绝族，759/918 场景） | 已覆盖 |
| I3 | §4.2 末 | Teacher 匿名治理最小披露 | community 矩阵 M10 全行（`tests/integration/community/test_anonymous_moderation.py` 四性质族）——互引不重复 | 已覆盖（互引 M10） |
| I4 | §4.3 + §5.8 + §5.7 | 邀请五步；创建/接受全审计；BANNED→ACTIVE 仅 Admin | `tests/integration/identity/test_staff_onboarding.py::test_admin_invites_teacher_records_hashed_single_use_token` / `test_expired_invitation_cannot_be_accepted` / `test_invitation_is_single_use` / `test_staff_invitation_created_writes_durable_audit_row` / `test_staff_invitation_accept_writes_durable_audit_row` / `test_non_admin_actor_cannot_create_invitation` + `tests/integration/admin/test_account_admin.py::test_legal_transition_commits_and_audits_in_one_transaction` / `test_illegal_transition_is_a_typed_409_and_writes_nothing` / `test_non_admin_actor_is_refused` | 已覆盖 |
| I5 | §5.1-5.2 | 学号规则族 + 七边界 + string 前导 0 + 白名单命中 + 并发唯一 | `tests/integration/identity/test_registration.py::test_register_rejects_invalid_student_number_format`（格式族）/ `test_register_leading_zero_student_number_round_trips` / `test_register_absent_whitelist_rejected` / `test_register_disabled_whitelist_rejected` / `test_concurrent_same_username_exactly_one_succeeds` + `tests/integration/identity/test_identity_constraints.py::test_duplicate_username_rejected` / `test_student_number_round_trips_as_string` / `test_duplicate_whitelist_student_number_rejected` | 已覆盖 |
| I6 | §5.3 | nickname 16 grapheme/控制字符/trim/emoji 不截断 | `tests/integration/identity/test_profile_contacts.py::test_change_nickname_enforces_16_grapheme_boundary` / `test_change_nickname_normalizes_and_persists` / `test_change_nickname_rejects_blank_after_cleaning` + `tests/unit/identity/test_validation.py`（CJK/ZWJ family emoji/combining 组合族） | 已覆盖 |
| I7 | §5.4 | OTP 绑定/E.164 唯一/换绑三验/并发 DB 兜底/脱敏 | `tests/integration/identity/test_profile_contacts.py::test_phone_change_request_requires_current_password` / `test_phone_change_request_sends_otp_to_new_phone_and_keeps_old` / `test_phone_change_request_rejects_phone_bound_elsewhere_or_self` / `test_phone_change_confirm_swaps_phone_and_frees_old` + `test_registration.py::test_concurrent_same_phone_exactly_one_succeeds` + `tests/integration/identity/test_identity_constraints.py::test_duplicate_normalized_phone_rejected`；脱敏：`tests/unit/integrations/test_masking.py`（mask 族）+ `tests/integration/identity/test_rate_limiter.py`（规范化键脱敏面） | 已覆盖 |
| I8 | §5.5 | 邮箱可选/verified 才用/规范化唯一/解绑不动手机 | `tests/integration/identity/test_identity_constraints.py::test_duplicate_non_null_email_rejected` / `test_multiple_null_emails_allowed` + `tests/integration/identity/test_profile_contacts.py`（bind/verify/unbind 族）+ `test_profile_contacts.py::test_unbind_email_requires_password_and_clears_address`；verified 门槛：`tests/unit/notifications/test_channel_eligibility.py::test_unverified_email_is_skipped_with_reason_not_failed`（互引 N3） | 已覆盖 |
| I9 | §5.6 密码 | Argon2id/10-128/改密找回后旧会话失效 | `tests/unit/identity/test_passwords.py::test_hash_is_argon2id_and_contains_no_plaintext` + `test_registration.py::test_register_password_outside_band_rejected` + `test_password_recovery.py::test_change_password_requires_current_password` / `test_change_password_revokes_other_sessions_keeps_current` / `test_reset_confirm_rotates_password_and_revokes_all_sessions` | 已覆盖 |
| I10 | §5.6 会话 | 短 access/可撤可轮换/服务端记录/CSRF | `tests/integration/identity/test_sessions.py::test_rotate_unknown_token_fails` / `test_rotate_within_expiry_boundary_succeeds` / `test_revoke_all_kills_live_sessions` / `test_revoke_session_logs_out_only_the_presented_token` + `tests/integration/identity/test_account_status.py::test_expired_access_token_rejected` / `test_revoked_session_access_token_rejected_before_expiry` / `test_rotated_session_kills_the_old_access_token` / `test_expired_session_row_rejects_fresh_access_token` + `tests/integration/identity/test_refresh_race_http.py`（轮换竞态）+ CSRF：`app/modules/identity/routing_common.py` 双提交契约（`tests/unit/identity/test_cookie_paths.py` 域内） | 已覆盖 |
| I11 | §5.6 Staff 2FA | 强制 TOTP；未完成不得进后台；恢复码 hash | `tests/integration/identity/test_identity_api.py::test_staff_onboarding_until_management_access`（TOTP_SETUP_REQUIRED 门 + 恢复码一次展示）+ `tests/integration/identity/test_account_status.py::test_confirmed_staff_passes_the_management_guard` + `tests/unit/identity/test_totp.py::test_hash_stores_no_plaintext_and_is_argon2id`（恢复码 hash） | 已覆盖 |
| I12 | §5.7 | 状态机五转移 + 冻结五影响 + 不物删 | `tests/integration/admin/test_account_admin.py::test_legal_transition_commits_and_audits_in_one_transaction` / `test_illegal_transition_is_a_typed_409_and_writes_nothing` / `test_full_governance_cycle_leaves_history_untouched`（保留）+ `tests/integration/identity/test_account_status.py::test_suspended_student_with_valid_token_denied_state_change` + `test_account_admin.py::test_non_active_account_with_live_token_is_refused_by_service_gates`；域级冻结影响：claims C5（领取门）/ submissions S3（上传门）/ community M4（评论门）互引 | 已覆盖 |
| I13 | §21.4 + G12 | Admin 揭示：权限+reason+每次审计 | `tests/integration/community/test_anonymous_moderation.py::test_reveal_requires_a_reason` / `test_reveal_is_admin_only` + e2e `tests/e2e/test_privacy_rbac.py::test_anonymous_privacy_and_explicit_reveal` | 已覆盖（community M11 的完整落位） |
| I14 | G10 | 元规则：RBAC=角色+所有权+状态；不确定默认拒 | ——（流程行：I1-I4 的正负空间矩阵即其执法面） | 流程行 |

## 缺口明细

无。

## 不匹配明细

无。

## 备注

- I12 的"冻结五影响"横跨四域（领取/上传/社区/积分审计保留），本矩阵引状态治理主行，域级行为在 claims/submissions/community 各矩阵已对账——互引不重复。
- `test_account_status.py::test_role_comes_from_the_user_row_not_the_jwt_claim` 是 G10"服务端解析角色"的额外加固证据（role 不信 token 声明）。
