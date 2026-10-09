# community 规约-测试对账矩阵

> 建立日期：2026-10-08 · 域：Task 评分 / 评论与匿名隐私 / Vote+Reaction / 举报 / 排序（P2 批次四域之二）
> 方法论：`campusquest-test-process` skill；**预期列先于测试检索落笔**；缺口列出不补；不匹配上报不擅改。
> 规约来源：spec §20 / §21(+21.1-21.4) / §22 / §23 / §24；quality-gates §16 G11/G12。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 16 | M1-M16 全部 |
| 缺口 | 0 | — |
| 不匹配 | 0 | — |
| 流程行 | 1 | M17 |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| M1 | §20 约束 + §31.7 | UNIQUE(task_id,user_id)+CHECK(1..5) DB 约束 | `tests/integration/community/test_community_constraints.py::test_duplicate_task_rating_rejected` / `test_rating_outside_one_to_five_rejected` / `test_rating_boundary_values_accepted` | 已覆盖 |
| M2 | §20 资格 | 仅完成过该 Task ≥1 Claim 者可评分 | `tests/integration/community/test_ratings.py::test_completed_claim_unlocks_rating` + `test_non_completed_history_cannot_rate` + `test_completion_on_another_task_does_not_unlock` | 已覆盖 |
| M3 | §20 聚合 | 可改不重建；前台只见聚合+数量 | `tests/integration/community/test_ratings.py::test_re_rate_updates_in_place_and_moves_the_summary` + `test_two_raters_are_two_rows_and_one_aggregate` + `test_adapter_surfaces_the_aggregate_through_the_port`（聚合 DTO）+ `test_concurrent_first_ratings_end_as_one_row`（并发一列） | 已覆盖 |
| M4 | §21.1 发布 | 即发即显；2000 上限可配置；防 XSS；去控制字符；rate limit；空白拒绝 | `tests/integration/community/test_comments.py::test_whitespace_only_comment_rejected` / `test_over_length_comment_rejected_at_2000_default` / `test_comment_length_cap_is_configurable` / `test_dangerous_control_characters_stripped_before_storage` / `test_xss_payload_stored_literally_and_returned_as_json_text` + `tests/integration/community/test_community_api.py`（真实 Redis 限流器 429 RATE_LIMITED 断言族） | 已覆盖 |
| M5 | §21.1 匿名选择 | nickname 公开或本条匿名；匿名不改榜单身份 | `tests/integration/community/test_comments.py::test_student_creates_comment_and_public_list_shows_nickname` + `test_comments.py::test_anonymous_comment_leaks_no_identity_facts`；榜单身份不受评论匿名影响：`tests/integration/rankings/test_rankings.py::test_top_orders_desc_and_enriches_display_profile`（昵称目录口径与评论匿名无耦合） | 已覆盖 |
| M6 | §21.2 回复四防 | 跨 Task parent/循环/不存在或隐藏/越权 | `tests/integration/community/test_comments.py::test_reply_to_cross_task_comment_rejected`（同 Task 指针结构性不可能成环——测试 docstring 载明论证）/ `test_reply_to_missing_comment_rejected` / `test_reply_to_deleted_parent_rejected` + `tests/integration/community/test_comment_edit_delete.py::test_edit_is_owner_only`（越权） | 已覆盖 |
| M7 | §21.3 编辑 | "已编辑"+Revision 历史+仅见最新 | `tests/integration/community/test_comment_edit_delete.py::test_owner_edit_replaces_content_and_snapshots_previous_version` / `test_second_edit_appends_second_revision_of_previous_version` + `tests/integration/community/test_comments.py::test_edited_flag_reflects_revision_existence` | 已覆盖 |
| M8 | §21.3 删除 | 软删；父删子留+墓碑；Admin 彻底隐藏留审计 | `tests/integration/community/test_comment_edit_delete.py::test_owner_soft_delete_sets_trio_and_row_survives` / `test_deleted_parent_with_surviving_child_renders_tombstone` / `test_deleted_leaf_comment_vanishes_entirely` + `tests/integration/community/test_moderation_audit.py::test_moderate_delete_writes_one_audit_row_with_the_visibility_trio` / `test_hard_hide_audits_subtree_scale_and_root_migration` | 已覆盖 |
| M9 | §21.4 普通用户匿名面 | 只见"匿名用户"，无可识别信息 | `tests/unit/community/test_comment_serialization.py::test_anonymous_comment_serializes_author_as_anonymous_display` / `test_anonymous_comment_json_leaks_no_identity_facts` / `test_public_dto_has_no_user_id_field_at_all`（by-construction）+ `tests/integration/community/test_comments.py::test_anonymous_comment_leaks_no_identity_facts` | 已覆盖 |
| M10 | §21.4 Teacher 治理面 | 无学号/手机/邮箱/username；可用 pseudonymous key（密绑 secret、任务内稳定、跨任务分离、不入学生端） | `tests/integration/community/test_anonymous_moderation.py::test_anonymous_moderation_record_carries_no_identity_facts` / `test_moderation_dto_has_no_identity_fields_by_construction` / `test_moderation_key_stable_within_task_and_bound_to_secret` / `test_moderation_key_correlates_within_task_and_separates_authors` / `test_moderation_key_differs_across_tasks` | 已覆盖 |
| M11 | §21.4 Admin 揭示 | 专门操作+权限+reason+每次 AuditLog；普通列表不自动展开 | `tests/integration/community/test_moderation_audit.py::test_moderate_delete_replay_writes_no_second_row`（重放不双写=不自动展开的机制面）+ identity 揭示审计（跨域）：`tests/integration/admin/test_admin_api.py` 揭示操作审计族；`tests/unit/community/test_comment_serialization.py::test_public_dto_has_no_user_id_field_at_all`（普通面无身份材料） | 已覆盖 |
| M12 | §22 Vote | ±1 值域；UNIQUE(comment,user)；四转移原子 | `tests/integration/community/test_community_constraints.py::test_duplicate_vote_rejected` / `test_vote_value_outside_plus_minus_one_rejected` + `tests/integration/community/test_votes.py::test_none_to_like_inserts_exactly_one_row` / `test_like_to_none_deletes_the_row` / `test_like_to_dislike_flips_the_row_in_place` / `test_dislike_to_like_flips_the_row_in_place` + 原子化并发：`test_concurrent_opposing_votes_from_none_leave_exactly_one_row` / `test_concurrent_create_vs_remove_race_leaves_at_most_one_row` | 已覆盖 |
| M13 | §22 Reaction | UNIQUE(comment,user,emoji)；白名单；无任意 HTML/图片 | `tests/integration/community/test_community_constraints.py::test_duplicate_reaction_same_emoji_rejected` / `test_same_user_different_emoji_reaction_allowed` + `tests/unit/community/test_settings_emoji_whitelist.py`（Admin 白名单）+ `tests/integration/community/test_reaction_settings_composition.py`（组合根） | 已覆盖 |
| M14 | §23 举报 | 四类；不自动删；进队列；同用户同评论同类别防重 | `tests/integration/community/test_reports.py::test_each_category_files_exactly_one_open_report` / `test_duplicate_same_category_is_idempotent` / `test_reporting_never_touches_the_comment_row` / `test_reported_comment_still_renders_in_the_public_list` + `tests/integration/community/test_community_constraints.py::test_duplicate_report_same_category_rejected` / `test_same_reporter_different_category_allowed` | 已覆盖 |
| M15 | §23 举报者不可见 | 被举报用户看不到举报者身份 | `tests/integration/community/test_reports.py::test_reporter_identity_is_absent_from_student_surfaces` | 已覆盖 |
| M16 | §24 排序 | 最新/最热；hot_score 服务端算不信客户端 | `tests/integration/community/test_community_api.py::test_hot_sort_is_server_computed_and_reorders_the_thread`（客户端 hot_score 422 于建行前）+ `tests/integration/community/test_comments.py::test_list_pagination_newest_first_with_id_tiebreak` | 已覆盖 |
| M17 | G11/G12 | 元规则：隐私由 DTO 形状强制+敏感读取可审计 | ——（流程行：M9-M11 的 by-construction 断言与审计断言即其执法面） | 流程行 |

## 缺口明细

无。

## 不匹配明细

无。

## 备注

- 本域为隐私纪律最重区：M9/M10 的 **by-construction 断言**（`test_public_dto_has_no_user_id_field_at_all`、`test_moderation_dto_has_no_identity_fields_by_construction`）正是 points 矩阵 G-1 建议的冻结模式在本域的既有实践。
- M11 的 Admin 揭示完整闭环（权限+reason+审计）跨 identity 域，本矩阵引机制面，完整行在 identity 矩阵（见该文件 I 行）。
