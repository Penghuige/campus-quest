# 创新创业私有项目草稿规约与测试

依据 `docs/superpowers/plans/2026-10-08-innovation-project-drafts.md`。以下预期从计划推导，先于测试实现写下；本表不宣称公开发布或负责人资格已经实现。

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| I1 | 计划：五字段草稿 | 仅名称必填，五字段trim并执行长度上限，拒绝owner/状态等额外字段 | `tests/unit/innovation/test_schemas.py::test_draft_content_validation` | 已覆盖 |
| I2 | 计划：本人管理接口 | 真实Bearer会话创建、重新读取、编辑，返回明确DTO，无owner/幂等指纹 | `tests/integration/innovation/test_project_drafts.py::test_owner_round_trip` | 已覆盖 |
| I3 | 计划：私有准备区 | 另一学生列表无此项，直链读写与未知ID同为404 | `tests/integration/innovation/test_project_drafts.py::test_other_student_cannot_read_or_write` | 已覆盖 |
| I4 | 计划：身份与Cookie边界 | 匿名及仅有效refresh Cookie时，列表/创建/详情/更新均401；停用学生403，教师/管理员403 | `tests/integration/innovation/test_project_drafts.py::test_authentication_and_account_gates` | 已覆盖 |
| I5 | 计划：创建幂等 | 相同键与原始内容重试返回当前行，不覆盖编辑；不同内容409 | `tests/integration/innovation/test_project_drafts.py::test_create_retry_preserves_later_edit` | 已覆盖 |
| I6 | G15及计划：真并发 | 独立连接同创建键仅一行；同version并发保存仅一胜一409 | `tests/integration/innovation/test_project_drafts.py::test_independent_sessions_enforce_idempotency_and_version` | 已覆盖 |
| I7 | 计划：真实PG保存 | 服务提交后独立会话能读取，Clock时间持久化；数据库拒绝无效长度与版本 | `tests/integration/innovation/test_project_drafts.py::test_independent_sessions_enforce_idempotency_and_version` + `tests/integration/innovation/test_project_drafts.py::test_database_constraints` | 已覆盖 |
| I8 | G11及计划：私有响应 | 私有响应禁止共享缓存，DTO字段冻结，不含owner/创建key/指纹 | `tests/unit/innovation/test_schemas.py::test_private_response_field_whitelist` + `tests/integration/innovation/test_project_drafts.py::test_owner_round_trip` | 已覆盖 |
| I9 | 计划：全栈测试清理接线 | clean_world删除账号前删本世界草稿，其他用户及其草稿保留；独立提交验证 | `tests/integration/innovation/test_world_cleanup.py::test_world_cleanup_removes_owned_drafts_and_preserves_other_world` | 已覆盖 |
