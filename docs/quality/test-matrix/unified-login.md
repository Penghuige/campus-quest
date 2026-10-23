# unified login 规约-测试对账矩阵

> 建立日期：2026-10-19 · 域：统一登录挑战族（spec 2026-10-19-unified-login-design §3.1/§3.2/§3.4，PR-1 后端半）
> 方法论：`campusquest-test-process` skill；**预期列先于测试检索落笔**；缺口列出不补；不匹配上报不擅改。
> 规约来源：unified-login-design §3（main 5e3636d 契约修订版）+ §5 安全边界；interfaces.md「Unified login challenge family」小节。
> 域界：legacy 端点族（PR-3 退役）不在本表；邀请接受流不动；/auth/register 与 /auth/mobile-code/register 不动。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 12 | U1-U12 |
| 缺口 | 0 | — |
| 不匹配 | 0 | — |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| U1 | §3.1 | request 四臂按标识符形态路由：邮箱→员工邮箱码（真发信）；手机按 users 行——无行/STUDENT→学生手机码、TEACHER/ADMIN→员工手机码（未注册手机真发码=发码即入口语义保持）；学号→422 格式层拒绝（无账号查询无枚举面）；响应恒 202 | `tests/integration/identity/test_unified_challenge.py::test_request_routes_by_identifier_shape` | 已覆盖 |
| U2 | §3.1 + #85/#101 纪律 | 发码类 202 前必须 commit 挑战行：savepoint 假绿不可证，真会话+独立连接读回（email 臂 login_email_challenges、手机臂 login_mobile_challenges）；清扫幂等 | `tests/integration/identity/test_unified_challenge.py::test_request_commits_challenges_across_connections` | 已覆盖 |
| U3 | §3.1 | verify 判别联合（判定只认 status）：email 臂 ADMIN→totp_required{pending_token}；统一 totp 端点（email 域 pending）→authenticated{role, tokens 嵌套 TokenPairResponse} | `tests/integration/identity/test_unified_challenge.py::test_verify_email_arm_admin_answers_totp_required` | 已覆盖 |
| U4 | §3.1 | 手机臂三态：STUDENT→authenticated、TEACHER→authenticated、未注册→unregistered{register_token}（内联注册三步复用现有语义） | `tests/integration/identity/test_unified_challenge.py::test_verify_phone_arms_answer_all_three_states` | 已覆盖 |
| U5 | §3.1 | 一个 totp 端点吃两族 pending 域（emaillogin:pending / staffmobilecode:pending；错域 GETDEL miss 不烧 token）；bogus pending→CHALLENGE_CODE_INVALID | `tests/integration/identity/test_unified_challenge.py::test_totp_endpoint_serves_both_pending_domains` | 已覆盖 |
| U6 | §3.4 | 错码/过期/耗尽合一→CHALLENGE_CODE_INVALID 400（分支理由只入服务端日志）；RATE_LIMITED/ACCOUNT_LOCKED/VALIDATION_ERROR 沿用 | `tests/integration/identity/test_unified_challenge.py::test_wrong_codes_answer_the_single_challenge_envelope` | 已覆盖 |
| U7 | §3.1 remembered（2026-10-20 放开） | GET /auth/challenge/remembered：全角色受众（学生 ACTIVE/PENDING_APPROVAL、员工 ACTIVE）→200{identifier_masked}（有手机 mask_phone 否则 mask_email，掩码不含全值）；无/坏 cookie、SUSPENDED→204；verify 空体（cookie 单因子）学生→authenticated | `tests/integration/identity/test_unified_challenge.py::test_remembered_probe_and_empty_body_verify_leg` / `::test_remembered_probe_serves_all_roles_with_a_masked_identifier`（五臂） | 已覆盖 |
| U8 | §5.1 裁定 | 空体 verify 对 ADMIN 恒 totp_required——cookie 不因码腿豁免二因子（免 TOTP 语义只在 /auth/login 密码腿的 cookie honoring）；探针侧员工 cookie 自 2026-10-20 起为受众（见 U7；探针只广告不豁免） | `tests/integration/identity/test_unified_challenge.py::test_empty_body_verify_still_owes_admin_the_second_factor` | 已覆盖 |
| U9 | §3.2 | login 标识形态：学号→学生密码流；手机（学生先手机后学号=#73 并入）→学生密码流；邮箱→员工密码流（TEACHER 密码即全认证 §2.3）；旧形态 {username,password} 并存收（PR-3 清） | `tests/integration/identity/test_unified_challenge.py::test_login_identifier_form_covers_three_identifiers` | 已覆盖 |
| U10 | §3.2 + §2.2 | ADMIN 密码腿：无 cookie→totp_required；有效 remembered cookie→authenticated 直落（staff/login 30 天重登语义保持）；cookie 经码流 totp 腿+remember_device 铸造 | `tests/integration/identity/test_unified_challenge.py::test_login_admin_leg_honors_the_remembered_cookie` | 已覆盖 |
| U11 | 裁定 c) 修订 2026-10-20（owner 统一页精简） | **全受众 remember_device 铸造**：TEACHER 码流 verify（邮箱/手机臂）勾选→铸 30 天设备凭据，空体腿免码直落（对齐 ADMIN cq_staff_device 语义）；ADMIN 不变（totp 腿发）；cookie 恒 30 天窗口（Set-Cookie max-age=30d 断言）；铸造类跨连接耐久（真会话+独立连接读回） | `tests/integration/identity/test_unified_challenge.py::test_teacher_email_code_remember_mints_and_reopens` / `test_teacher_mobile_code_remember_mints` / `test_every_remembered_cookie_rides_a_30_day_window` / `test_teacher_remember_mint_survives_a_fresh_connection` | 已覆盖 |
| U12 | §3.2 #73 语义（owner 测试账号 bug 2026-10-23） | **10 位学号 vs 手机形状歧义**：phonenumbers 视 10 位数字为合法 CN 固话——密码腿 phone 臂 row 缺失时回退学号登录（双真实样例 2099000001/2021023456；错密码走 login_student 统一 401）；码门（request）phone 臂 row 缺失且 username 命中→422 学号引导「学号请使用密码登录」且零短信；未注册 10 位数仍 202 发码（入口流不变）；verify 同形防御（陈旧挑战不得转化学号→入口流，恒 CHALLENGE_CODE_INVALID）；8 位学号回归不动 | `tests/integration/identity/test_unified_challenge.py::test_ten_digit_student_numbers_password_login` / `test_ten_digit_student_number_wrong_password_is_uniform` / `test_eight_digit_student_number_still_logs_in` / `test_ten_digit_student_number_is_refused_on_the_code_door` / `test_unregistered_ten_digit_number_still_enters_the_code_flow` / `test_stale_challenge_for_a_ten_digit_student_number_is_refused` | 已覆盖 |

## 缺口明细

无。

## 不匹配明细

无。
