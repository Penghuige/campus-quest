# unified login 规约-测试对账矩阵

> 建立日期：2026-10-19 · 域：统一登录挑战族（spec 2026-10-19-unified-login-design §3.1/§3.2/§3.4，PR-1 后端半）；2026-10-23 扩域：切换账号 popover 批（owner 反馈，前端专属）；2026-10-24 扩域：多账号凭据槽批（owner 反馈「切换账号仍要验证码」，后端半）
> 方法论：`campusquest-test-process` skill；**预期列先于测试检索落笔**；缺口列出不补；不匹配上报不擅改。
> 规约来源：unified-login-design §3（main 5e3636d 契约修订版 + 2026-10-24 §3.1 多槽修订）+ §5 安全边界；interfaces.md「Unified login challenge family」与「Remembered-device credential slots」小节；popover 批=owner 2026-10-23 交互定案（会话记录，五条）。
> 域界：legacy 端点族（PR-3 退役）不在本表；邀请接受流不动；/auth/register 与 /auth/mobile-code/register 不动。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 20 | U1-U20 |
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
| U7 | §3.1 remembered（2026-10-20 放开） | GET /auth/challenge/remembered：全角色受众（学生 ACTIVE/PENDING_APPROVAL、员工 ACTIVE）→200{identifier_masked}（有手机 mask_phone 否则 mask_email；email_normalized 空时兜底 username 含 @ 的邮箱，掩码不含全值）；无/坏 cookie、SUSPENDED→204；verify 空体（cookie 单因子）学生→authenticated | `tests/integration/identity/test_unified_challenge.py::test_remembered_probe_and_empty_body_verify_leg` / `::test_remembered_probe_serves_all_roles_with_a_masked_identifier`（五臂） | 已覆盖 |
| U8 | §5.1 裁定 | 空体 verify 对 ADMIN 恒 totp_required——cookie 不因码腿豁免二因子（免 TOTP 语义只在 /auth/login 密码腿的 cookie honoring）；探针侧员工 cookie 自 2026-10-20 起为受众（见 U7；探针只广告不豁免） | `tests/integration/identity/test_unified_challenge.py::test_empty_body_verify_still_owes_admin_the_second_factor` | 已覆盖 |
| U9 | §3.2 | login 标识形态：学号→学生密码流；手机（学生先手机后学号=#73 并入）→学生密码流；邮箱→员工密码流（TEACHER 密码即全认证 §2.3）；旧形态 {username,password} 并存收（PR-3 清） | `tests/integration/identity/test_unified_challenge.py::test_login_identifier_form_covers_three_identifiers` | 已覆盖 |
| U10 | §3.2 + §2.2 | ADMIN 密码腿：无 cookie→totp_required；有效 remembered cookie→authenticated 直落（staff/login 30 天重登语义保持）；cookie 经码流 totp 腿+remember_device 铸造 | `tests/integration/identity/test_unified_challenge.py::test_login_admin_leg_honors_the_remembered_cookie` | 已覆盖 |
| U11 | 裁定 c) 修订 2026-10-20（owner 统一页精简） | **全受众 remember_device 铸造**：TEACHER 码流 verify（邮箱/手机臂）勾选→铸 30 天设备凭据，空体腿免码直落（对齐 ADMIN cq_staff_device 语义）；ADMIN 不变（totp 腿发）；cookie 恒 30 天窗口（Set-Cookie max-age=30d 断言）；铸造类跨连接耐久（真会话+独立连接读回） | `tests/integration/identity/test_unified_challenge.py::test_teacher_email_code_remember_mints_and_reopens` / `test_teacher_mobile_code_remember_mints` / `test_every_remembered_cookie_rides_a_30_day_window` / `test_teacher_remember_mint_survives_a_fresh_connection` | 已覆盖 |
| U12 | §3.2 #73 语义（owner 测试账号 bug 2026-10-23） | **10 位学号 vs 手机形状歧义**：phonenumbers 视 10 位数字为合法 CN 固话——密码腿 phone 臂 row 缺失时回退学号登录（双真实样例 2099000001/2021023456；错密码走 login_student 统一 401）；码门（request）phone 臂 row 缺失且 username 命中→422 学号引导「学号请使用密码登录」且零短信；未注册 10 位数仍 202 发码（入口流不变）；verify 同形防御（陈旧挑战不得转化学号→入口流，恒 CHALLENGE_CODE_INVALID）；8 位学号回归不动 | `tests/integration/identity/test_unified_challenge.py::test_ten_digit_student_numbers_password_login` / `test_ten_digit_student_number_wrong_password_is_uniform` / `test_eight_digit_student_number_still_logs_in` / `test_ten_digit_student_number_is_refused_on_the_code_door` / `test_unregistered_ten_digit_number_still_enters_the_code_flow` / `test_stale_challenge_for_a_ten_digit_student_number_is_refused` | 已覆盖 |
| U13 | popover 批定案 1/2（owner 2026-10-23） | 顶栏「切换账号」点击→**原地 popover 菜单**（不登出不跳页）：列最近登录（cq.recent-logins）**排除当前账号**（jar 头=当前登录：一切成功路径经 recordLogin 漏斗 upsert 到头；邀请接受为漏斗外唯一登录，最坏隐藏一个陈旧选项）、≤4、掩码+角色徽标+昵称（与登录页卡片同渲染件 RecentLoginCardButton）；底部「使用其他账号」走原直跳；菜单为空（本机无其他账号）→点击直接原行为兜底 | 单测：`frontend/src/__tests__/switch-account.test.ts`（头排除/≤4 截断/单账号空三钉）；e2e：`frontend/e2e/switch-account.spec.ts`（链 C：菜单原地出现、教师卡在列、学生自身卡不在；链 A/B：单账号 `.switch-menu` 零元素=兜底直跳） | 已覆盖 |
| U14 | popover 批定案 3/4（owner 2026-10-23） | 点选目标账号→登出→**sessionStorage['cq.switch-pick']=identifier**（非 URL query，identifier 不入浏览器历史）→/login；UnifiedLoginPanel 挂载读 switch-pick→recent 找 entry→自动触发 onPickCard（#128 智能分流复用：免登直进/自动发码/学号转密码）→**读后即删**（一次性；未命中 identifier 平凡降级）；登出契约不动 cq_staff_device | 单测：`frontend/src/__tests__/switch-account.test.ts`（键名钉 cq.switch-pick、read-once 删除、缺失/受阻容忍）；e2e：链 C 尾段（选中教师卡→免登直进教师台、零发码请求=智能分流快道）；自动发码/密码转交臂由 #128 既有链覆盖（复用同一 onPickCard） | 已覆盖 |
| U15 | popover 批定案 5 + 键盘红线 | 菜单为浮层不入像素基线（触发按钮=#134 面不变）；focus-visible 全局环+Escape 关闭归还焦点+开菜单聚焦首项（有标签区域+原生按钮=屏幕阅读器可走，非 menu-role 方向键契约）；scrim 捕获外点 | `frontend/e2e/switch-account.spec.ts`（链 C：`.switch-menu` 结构面+URL 不动钉浮层性）+ visual 轮稳定面零变化（18/18 基线面不含浮层）；键盘行为由实现件 `frontend/src/components/shell/SwitchAccountButton.tsx` 的 focus/Escape 契约承载（无独立 e2e 断言——**行为级注记**） | 已覆盖（行为级） |
| U16 | 多槽批根修（owner 2026-10-24「切换账号仍要验证码」） | **槽并存**：凭据 cookie 按账号分槽 `cq_dev_<sha256(canonical identifier)[:10]>`（canonical 链=phone→email→username 含 @，同账号跨登录形态恒同名）；记 B 不覆 A——jar 双槽并存、探针双项、target 回开 A 免码；同账号跨腿（邮箱码→密码腿）重记**同名槽**（Set-Cookie 同名=jar 覆盖，探针单条目） | `tests/integration/identity/test_unified_challenge.py::test_remembering_a_second_account_leaves_the_first_slot_alive` / `::test_same_account_remember_keeps_exactly_one_slot` | 已覆盖 |
| U17 | 多槽批探针数组 | 探针 `200 {"remembered":[{"identifier_masked","slot"}…]}`（槽句柄=cookie 名后缀，掩码同 U7 链）；按最近序（`last_used_at` or `created_at` desc）≤5；**枚举不 bump last_used_at**（排序信号只被真实登录消耗——bump 仅选中槽）；空仍 204 | `test_unified_challenge.py::test_probe_answers_the_array_and_orders_by_recency` / `::test_probe_caps_the_array_at_five_entries` | 已覆盖 |
| U18 | 多槽批空体 target | 空体 verify 增 `{target?: slot}`：**无 target=最近槽**（判定基准与探针排序一致=横幅首位即点击所登，reviewer 裁定 2026-10-24；仅选中槽 bump）；target 命中→登该账号；ADMIN 槽仍 totp_required；target 无匹配槽（缺席/失效/他人）→单码 CHALLENGE_CODE_INVALID（无槽枚举面） | `test_unified_challenge.py::test_empty_body_verify_without_target_takes_the_most_recent_slot` / `::test_empty_body_verify_with_target_logs_into_that_slot` / `::test_admin_target_slot_still_owes_totp` / `::test_empty_body_verify_target_without_a_matching_slot_is_the_uniform_invalid` | 已覆盖 |
| U19 | 多槽批旧名退役+模块面 | 旧单名 `cq_staff_device` 不再读（**有效值**旧名呈递→探针 204+空体 400 CHALLENGE_CODE_INVALID；持有者重登一次可接受）；模块件：slot_suffix 稳定 10 位句柄/canonical 链三臂+None/slots_from 只收 cq_dev_*/issue 返回 (value,slot) 只存哈希/resolve_remembered 排序-不 bump-改名账号双槽去重-过期剔除/validate bump 语义保留 | `::test_the_retired_staff_cookie_name_is_no_longer_read` + `tests/integration/identity/test_device_credentials.py`（15 件） | 已覆盖 |
| U20 | 多槽批 honoring 槽化 | 密码腿 honoring=任意在座槽值**归属**到目标账号（槽名不参与判定——改名后仍 honoring；命中槽 bump、他人槽不 bump）；本账号槽在座→ADMIN 密码直落；仅他人槽在座→totp 照欠；mobile 服务两条空 code 旧 cookie 腿退役（facade 唯一调用方从不传空 code） | `::test_staff_password_honoring_reads_only_its_own_slot`（跨 U10 腿保留于既有件） | 已覆盖 |

## 缺口明细

无。

## 不匹配明细

无。
