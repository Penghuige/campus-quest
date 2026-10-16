# 注册审批与手机码登录 规约-测试对账矩阵

> 建立日期：2026-10-16 · 域：开放注册/PENDING_APPROVAL 冻结面/审批裁决/手机码登录/白名单退役
> 方法论：`campusquest-test-process` skill 规约对账节；**预期列先于测试检索落笔**。
> 规约来源：docs/superpowers/specs/2026-10-16-registration-approval-and-mobile-login-design.md §1-§6（owner 四决策定稿）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 14 | RA1-RA14 |
| 缺口（轻，列出不补） | 0 | — |
| 不匹配 | 0 | — |
| 流程行 | 0 | — |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| RA1 | §1 注册 | 白名单 gate 删除：任意学号+有效手机挑战即可注册；nickname 缺省=学号；password 可选（None/空串=NULL 无密码通道；提供则过 band） | `tests/integration/identity/test_registration.py::test_register_student_succeeds_pending_approval` / `::test_register_without_password_lands_null_hash` / `::test_register_blank_string_password_is_no_channel` / `::test_register_blank_nickname_defaults_to_student_number` / `::test_register_supplied_bad_password_still_rejected` | 已覆盖 |
| RA2 | §1 状态机 | 注册落 PENDING_APPROVAL（非 ACTIVE）；approved_at/rejected_reason 落库为空 | `test_registration.py::test_register_student_succeeds_pending_approval` + `tests/integration/identity/test_approval_status_machine.py::test_pending_approval_status_roundtrip` / `::test_rejected_reason_roundtrip` | 已覆盖 |
| RA3 | §2 可登录 | PENDING_APPROVAL 学生登录（密码/手机码通道）发正常会话；`/me` 返回 status | `tests/integration/identity/test_mobile_code_login_api.py::test_pending_approval_student_logs_in_end_to_end` + `session_service` 放行（`test_identity_api.py::test_student_flow_*` 状态断言） | 已覆盖 |
| RA4 | §2 冻结面 | 领取/社区写/头像 403 `APPROVAL_PENDING`；approve 后不再拒 | `tests/integration/identity/test_approval_gate_api.py::test_pending_student_frozen_across_acting_surfaces` / `::test_admin_approve_unlocks_and_audits` | 已覆盖 |
| RA5 | §2 浏览放行 | 任务广场 GET 对 PENDING 学生 200（require_active_actor 放行） | `tests/integration/identity/test_approval_gate_api.py::test_pending_student_frozen_across_acting_surfaces`（浏览断言在前） | 已覆盖 |
| RA6 | §2 审批 | approve→ACTIVE+approved_at+`USER_APPROVED` 审计；reject{reason?}→BANNED+rejected_reason+`USER_REJECTED`；重复/非 PENDING 目标 409 CONFLICT | `tests/integration/identity/test_approval_gate_api.py::test_admin_approve_unlocks_and_audits` / `::test_admin_reject_bans_with_reason` / `::test_double_approve_is_a_conflict` | 已覆盖 |
| RA7 | §2/§5 队列 | 待审列表=现有 `GET /admin/users?status=PENDING_APPROVAL` 过滤器 | `tests/integration/identity/test_approval_gate_api.py::test_pending_list_via_users_filter` | 已覆盖 |
| RA8 | §3 防枚举 | request 恒 202 空体（unknown/非学生/封禁同形）；60s 冷却任意手机统一 429；仅真学生被 SMS | `tests/integration/identity/test_mobile_code_login_api.py::test_request_is_constant_202_and_uniform_429` / `::test_non_student_or_inactive_phone_is_noop` | 已覆盖 |
| RA9 | §3 挑战 | 6 位/5 分钟/5 次尝试；错 5 次后正确码=ACCOUNT_LOCKED（retry_after_seconds）；keyed HMAC digest（#88 工艺） | `tests/integration/identity/test_mobile_code_login_api.py::test_wrong_code_five_times_then_lockout_envelope` + `tests/integration/identity/test_approval_status_machine.py::test_mobile_challenge_model_roundtrip` | 已覆盖 |
| RA10 | §3 记住设备 | 有效 cq_staff_device cookie 免码 verify（无 code 字段）→tokens+新 30 天值（恒续签/轮换）；remember_device 带码腿签发 | `tests/integration/identity/test_mobile_code_login_api.py::test_remembered_device_cookie_logs_in_without_any_code` | 已覆盖 |
| RA11 | §3 手机即登录名 | phone→user 唯一定位（唯一约束）；E.164 规范化 | `tests/integration/identity/test_mobile_code_login_api.py` 全族（find_by_phone_e164） | 已覆盖 |
| RA12 | §3 限速 | 手机+IP 双桶（auth:mobile-code/-ip）挂 request；429 不计锁定 | `tests/integration/identity/test_mobile_code_login_api.py::test_request_is_constant_202_and_uniform_429`（行为级注记：桶表在 rate_limit.py，路径经 limiter 注入） | 已覆盖（行为级） |
| RA13 | §6 白名单退役 | 4 admin 端点删路由（404）；STUDENT_NOT_WHITELISTED 错误码注销（interfaces.md+冻结名单）；表留库不迁移 | admin API 矩阵行删除（`tests/integration/admin/test_admin_api.py` 权限矩阵现集）+ `tests/unit/core/test_error_codes.py` 冻结名单不含该码 | 已覆盖 |
| RA14 | §6 密码通道 | 无密码账号走密码通道=统一 401（#83 F2 现状继承，四重认证点） | `tests/integration/identity/test_invitation_accept_no_totp.py::test_passwordless_teacher_on_password_channel_is_uniform_401`（同型面）+ login_student None-hash 分支 | 已覆盖 |
