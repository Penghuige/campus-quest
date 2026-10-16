# Registration Approval & Mobile Login Design（V1.1 认证扩展 II）

> Status: DRAFT — awaiting owner review
> Date: 2026-10-16 · Amends: spec §5（注册/登录/状态机）；关联 2026-10-15-v1.1 spec §2
> Owner decisions (2026-10-16): 审批前可登录但功能冻结 · 手机号即登录名 · 学生不用邮箱码 · 教师免 TOTP（现状）管理员保留双因子

## 0. 变更总览

| 项 | 现状 | 目标 |
|---|---|---|
| 学生注册 gate | 学号白名单（现空表=通道关死） | **取消白名单**：任何人 学号+手机短信码 注册 |
| 注册后状态 | 直接 ACTIVE | 新状态 **PENDING_APPROVAL**：可登录浏览，功能冻结 |
| 审批 | 无 | 管理台待审批队列：通过→ACTIVE / 拒绝→BANNED（附理由可选） |
| 学生登录 | 学号+密码 | **手机号+短信码**（默认）或 学号+密码（设了密码的） |
| 密码 | 注册强制 | **不强制**；不设= `password_hash` NULL（0028 已支持） |
| 默认密码 | — | **不生成假默认密码**：NULL 即"密码通道不可用"（假默认密码有暴力破解面+语义混乱；效果与 owner 意图一致——该账号只能手机码登录，之后可在设置里自主设密码） |
| 白名单管理页 | /admin/whitelist | **退役**（入口移除+后端 gate 删除；历史数据留库不迁移） |
| TOTP | 教师已免（Wave 1）/ADMIN 双因子 | 零改动（现状确认） |

## 1. 注册流（学生）

`POST /auth/register`：学号 + 手机号 + 短信码（保留现有手机验证挑战）+ 昵称（可选，默认=学号）+ **密码可选**（提交则为该账号开启密码通道）。
- 白名单查询路径整体删除（`require_enabled` 及调用链）。
- 成功 → `User.status = PENDING_APPROVAL`（**用户状态机新枚举值**，alembic 0030：状态 CHECK 扩值 + `approved_at`/`rejected_reason` 可空列）。
- 防滥用：注册 IP 限速沿用（5/h）；同手机号唯一性预检（现有）。

## 2. PENDING_APPROVAL 语义（功能冻结）

- **可登录**（手机码/密码通道均发正常会话）；`/me` 返回 status 供前端横幅（"账号待管理员审核，期间可浏览"）。
- **冻结面（后端 403 `APPROVAL_PENDING` 类型化错误码）**：领取任务、提交、兑换、评论/点赞/表情/评分/举报、头像修改。浏览面（任务广场/详情/排行榜/通知）放行。
- 实现：功能端点守卫链插入 approval gate（`require_approved_student`——仅对 STUDENT 且 PENDING_APPROVAL 拒绝；员工端点不受影响）。冻结清单以端点清单表进 spec 附录（实现时逐端点核对）。
- 管理员审批动作：`POST /admin/users/{id}/approve`（→ACTIVE，审计 `USER_APPROVED`）/ `POST /admin/users/{id}/reject` {reason?}（→BANNED，审计 `USER_REJECTED`）。

## 3. 手机验证码登录（学生）

- `POST /auth/mobile-code/request` {phone}（恒 202 防枚举；60s 冷却；手机+IP 双桶）→ `POST /auth/mobile-code/verify` {phone, code}（6 位/5 分钟/5 次尝试/账号锁定统一接入）。
- 挑战存储镜像 LoginEmailChallenge（`login_mobile_challenges`，HMAC 工艺同 #88 裁定）；发码走既有 SMS port（生产 aliyun 已接）。
- 手机号即登录名：登录时 `phone → user` 唯一定位（手机号唯一性现有约束）。
- **SMS 成本提示**：每次登录一条短信（阿里云按条计费）。缓解：登录成功签发 30 天"记住此设备"（复用 #87 cookie 工艺，学生同样适用——学生手机码=单因子，记忆设备=免短信重复发码，安全等价于密码会话时长）。

## 4. 登录页面板

- 学生页：默认"手机验证码登录"（手机号+码），折叠"密码登录"（学号+密码）。
- 员工页：现状（邮箱码默认/密码+TOTP 折叠）不动。
- 无密码账号在密码通道提交 → 统一 401（#83 F2 现状），前端文案引导"使用手机验证码登录或先设置密码"。

## 5. 管理台

- 新「注册审批」页（/admin/approvals）：PENDING_APPROVAL 学生列表（学号/手机掩码/昵称/注册时间）+单个通过/拒绝+**批量通过**；审计行随动作。
- NAV 挂入（替换退役的白名单入口位置）。
- 用户详情（Wave 2a 未做前）暂以列表+现有用户表状态展示。

## 6. 测试与工件（G19 摘要）

- 状态机：注册→PENDING→approve→ACTIVE 全链；reject→BANNED；PENDING 冻结面逐端点 403 断言（清单表驱动）；浏览面放行断言。
- 手机码：防枚举恒 202/冷却/5 次/锁定/记住设备免码；SMS fake 注入测试。
- 注册：无密码账号（NULL）手机码全链+密码通道 401；带密码注册双通道。
- 白名单：gate 删除后原白名单端点 404/退役；e2e 学生注册→横幅→审批→解锁全链。
- 矩阵/契约/基线照走；OpenAPI+TS 同 PR。

## 7. 非目标

学生邮箱码（owner 拒）；修改手机号流程（现有，不动）；批量导入学号（白名单退役后无意义）；Wave 2a 权力包重叠部分（用户详情等）各自交付。
