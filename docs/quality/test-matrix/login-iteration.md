# 登录页迭代（owner 四项反馈） 规约-测试对账矩阵

> 建立日期：2026-10-17 · 域：注册即登录/staff 记住设备重登/免码提示条探测/cookie-only 免码腿（①员工绑定手机+员工手机码另批对账）
> 方法论：`campusquest-test-process` skill 规约对账节；**预期列先于测试检索落笔**。
> 规约来源：owner 2026-10-17 登录页四项反馈 + 前端契约直连冻结记录（2026-10-17 跨会话），基线 spec §5/§5.6/§5.8、V1.1 §2.2 remembered-device 工艺（#87）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 9 | LI1-LI9 |
| 缺口（轻，列出不补） | 0 | — |
| 不匹配 | 0 | — |
| 流程行 | 0 | — |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| LI1 | ④ 注册即登录 | POST /auth/register 201 body=`{user: UserPublic, tokens: TokenPairResponse}` 包络；tokens=登录同款（access/csrf/token_type，refresh 永不进 body） | `tests/integration/identity/test_register_auto_login_api.py::test_register_answers_envelope_with_usable_session` | 已覆盖 |
| LI2 | ④ cookie 集 | 注册响应 Set-Cookie 集=登录路径（refresh_token+csrf_token+cq_shell），PENDING_APPROVAL 也发（#96 直进浏览冻结态） | `tests/integration/identity/test_register_auto_login_api.py::test_register_answers_envelope_with_usable_session`（set-cookie 断言） | 已覆盖 |
| LI3 | ④ 即用会话 | 注册响应的 access_token 直接可用：GET /me 200 且 status=PENDING_APPROVAL（消除注册即登录撞 60s 短信冷却） | `tests/integration/identity/test_register_auto_login_api.py::test_register_answers_envelope_with_usable_session`（/me 断言） | 已覆盖 |
| LI4 | ② remember_device | StaffLoginRequest 增 remember_device（缺省 false 行为不变）；勾选+完整登录成功→Set-Cookie cq_staff_device 30 天 | `tests/integration/identity/test_staff_login_remembered_api.py::test_full_login_with_remember_mints_device_cookie` / `::test_nonempty_code_without_cookie_still_logs_in` | 已覆盖 |
| LI5 | ② 空码重登 | totp_code 空串/缺省等价：空码+有效 cq_staff_device（绑本人）→密码对即完整登录（cookie=第二因子，同 email-code ADMIN 腿语义）+cookie 恒续签轮换 | `tests/integration/identity/test_staff_login_remembered_api.py::test_empty_code_with_valid_cookie_completes_login` | 已覆盖 |
| LI6 | ② 防枚举 | 空码+无 cookie/过期 cookie→uniform AUTHENTICATION_REQUIRED，错误体与错密码完全一致（除 request_id）；setup-required 信号不泄露 | `tests/integration/identity/test_staff_login_remembered_api.py::test_empty_code_without_cookie_is_uniform_authentication_failure` / `::test_empty_code_with_expired_cookie_is_uniform_failure` | 已覆盖 |
| LI7 | ③ 探测命中 | GET /auth/mobile-code/remembered：cookie 绑 STUDENT（ACTIVE 或 PENDING_APPROVAL）且有绑定手机→200 `{phone_masked}`（mask_phone 家族：E.164 前3+****+后4）；掩码=绑定用户手机（行绑 user.id） | `tests/integration/identity/test_mobile_remembered_probe_api.py::test_probe_answers_masked_phone_for_active_student` / `::test_probe_answers_pending_student_too` | 已覆盖 |
| LI8 | ③ 探测恒 204 | 无 cookie/未知值/过期/绑 staff→恒 204 空体（存在性永不作答；staff 绑定在学生登录页不渲染 banner） | `tests/integration/identity/test_mobile_remembered_probe_api.py::test_probe_is_empty_204_for_every_non_banner_shape` | 已覆盖 |
| LI9 | ③ cookie-only 腿 | POST /auth/mobile-code/verify 空体 {}+cookie→200 TokenPairResponse+新 30 天 cookie（#87 恒续签轮换，device 行直定位 user，掩码不可逆无全号）；空体无 cookie→uniform 400 MOBILE_CODE_INVALID | `tests/integration/identity/test_mobile_remembered_probe_api.py::test_cookie_only_verify_opens_session_and_rotates_cookie` / `::test_cookie_only_verify_without_cookie_is_uniform_400` | 已覆盖 |
