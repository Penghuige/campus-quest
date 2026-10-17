# 注册审批与手机码登录 规约-测试对账矩阵

> 建立日期：2026-10-16 · 域：开放注册/PENDING_APPROVAL 冻结面/审批裁决/手机码登录/白名单退役
> 方法论：`campusquest-test-process` skill 规约对账节；**预期列先于测试检索落笔**。
> 规约来源：docs/superpowers/specs/2026-10-16-registration-approval-and-mobile-login-design.md §1-§6（owner 四决策定稿）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 14 | RA1-RA14 |
| 前端已覆盖（后端半随其 PR） | 3 | RA15-RA17（登录迭代批次 2026-10-17：冻结契约先行，前端钉就位，后端集成行由后端半 PR 落） |
| 前端已覆盖（①；后端钉随 #101） | 2 | RA18-RA19（登录迭代 ①：/auth/staff/mobile-code 族+/me/phone/bind 族，PR #101 含后端钉） |
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
| RA15 | 登录迭代④（2026-10-17 owner 反馈；冻结契约） | register 201 答 `{user, tokens}` 包络（tokens=登录同款，refresh 恒走 HttpOnly cookie）；注册即登录直进学生首页（PENDING=浏览冻结态首页），消除注册后撞 60s 短信冷却；过渡期裸 UserPublic 走 legacy `/login?registered=1` 跳转，随后端半退役 | 前端：`frontend/src/__tests__/auth-api.test.ts`（envelope 采纳钉+legacy 钉）+ `e2e/auth.spec.ts`/`auth-extension-2.spec.ts`/`nickname-nudge.spec.ts` 直进断言；后端半集成行由后端 PR 落 | 前端已覆盖；后端半待其 PR |
| RA16 | 登录迭代②（同上） | staff/login 增 `remember_device`（缺省=false 行为不变；勾选=验过第二因子后签 30 天 cq_staff_device）；重登腿 `totp_code:""`≡缺省——cookie 命中+密码过=完整登录，未命中=uniform AUTHENTICATION_REQUIRED（与错密码同码同文案）；学生密码通道不加（无第二因子，30 天会话已是默认） | 前端：`frontend/src/__tests__/staff-auth-api.test.ts`（remember presence-based 钉+空串钉）+ `staff-auth.test.ts`（空因子放行钉）+ `e2e/staff-auth.spec.ts` 全链（勾选登→登出→空码重登）；后端半集成行由后端 PR 落 | 前端已覆盖；后端半待其 PR |
| RA17 | 登录迭代③（同上） | `GET /auth/mobile-code/remembered` 匿名探测：cookie 命中绑定 STUDENT（ACTIVE/PENDING_APPROVAL、有手机）→200 `{phone_masked}`（mask_phone 形态），其余恒 204 零泄露；banner 仅探测有掩码时渲染（无 cookie 登录页零多余元素）；点击=空体免码 verify（cookie-only 定位用户），拒绝态提示条换诚实文案非报错 | 前端：`frontend/src/__tests__/mobile-login-api.test.ts`（探测三态钉+空体点击腿钉）+ `e2e/auth-extension-2.spec.ts` 两态钉（有 cookie 见掩码一键落地/无 cookie 零元素）；后端半集成行由后端 PR 落 | 前端已覆盖；后端半待其 PR |
| RA18 | 登录迭代①（2026-10-17；冻结契约 PR #101） | 员工手机码登录族 `/auth/staff/mobile-code/*`：request {phone} 恒 202 防枚举；verify {phone, code}（**TEACHER 完成腿永不带 remember_device**——2026-10-15 终局裁决沿承）→tokens 或 ADMIN totp_required；totp 腿 {pending_token, code, remember_device?} body 携带、checkbox 仅此腿 | `frontend/src/__tests__/staff-auth-api.test.ts`（三端点线缆钉含 remember presence-based 两态）+ `frontend/src/__tests__/staff-email-code-login-view.test.ts`（describeMobileStaffLoginError 码分支钉）+ `frontend/e2e/staff-mobile-code-login.spec.ts`（teacher 一步/admin 两腿全链）；后端集成行随 #101 | 前端已覆盖；#101 已含后端钉 |
| RA19 | 登录迭代①绑定流 | `/me/phone/bind` {phone}→ChallengeResponse（202）/`confirm` {challenge_id, code}→MePublic；staff 专属（学生 403 走原 password-gated change 族）；首绑/换绑同流；409 PHONE_ALREADY_BOUND→「该手机号已绑定其他账号」字段位文案；无需密码再认证（员工会话即权威，email 绑定先例） | `frontend/src/__tests__/staff-auth-api.test.ts`（bind 族线缆钉+409 码钉）+ `frontend/e2e/staff-mobile-code-login.spec.ts`（绑定→409 换绑全链）；后端集成行随 #101 | 前端已覆盖；#101 已含后端钉 |
