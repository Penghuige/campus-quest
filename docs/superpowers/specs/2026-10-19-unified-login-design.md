# 统一登录页设计（unified login）

- **状态**：Approved — owner 2026-10-19 批准（"可以，做吧"）
- **取代**：#113 员工页两通道改版（PR 关闭 superseded，组件工作延续复用）
- **吸收**：#73 学生密码双标识（先手机后学号）、#74 员工统一验证码通道——两批后端半并入本批
- **关联**：`2026-10-15-v1.1-experience-and-administration-design.md` §5.6（登录通道语义保持）；`2026-10-16-registration-approval-and-mobile-login-design.md`（发码即入口/手机码登录，语义保持）

## 1. 背景与目标

登录页四项迭代后，学生页为二选 toggle（手机验证码/密码），员工页为三选 toggle（邮箱验证码/手机验证码/密码），选择面过宽。owner 定案：**两页并一页**，唯一标识框 + 渐进披露，必要操作仅在条件满足时出现。

**非目标**：不改挑战存储/冷却/锁定/设备凭据/TOTP 工艺（既有纪律全部保持）；不改邀请接受流（`/staff/invite/{token}` 独立）；不加新登录通道。

## 2. 数据模型依据（已核实）

users 单表三角色（STUDENT/TEACHER/ADMIN）；`phone_e164`、`email_normalized` 均为全局部分唯一索引（`backend/app/modules/identity/models.py:89-101`）→ **标识符→账号映射无歧义，同号/同邮箱双身份不可能存在**。学号=学生专属标识。邮箱登录仅员工（owner 既定决策：学生无邮箱码）。

## 3. API 契约

### 3.1 统一挑战端点（新增）

- `POST /auth/challenge/request` — body `{identifier}`
  - identifier：E.164 手机号或邮箱。学号 → 422 格式校验拒绝（格式层校验，非账号查询，无枚举面）。
  - **内部路由（路由 facade，存储与工艺不动）**：
    - 邮箱 → 员工邮箱码服务（防枚举恒 202 + 冷却不变）
    - 手机号 → 按 users 行：无行 → 学生注册挑战（发码即入口）；STUDENT → 学生手机码挑战；TEACHER/ADMIN → 员工手机码挑战
  - 响应恒 202 同构（冷却 429 信封不变）。前端提示按**自己提交的标识格式**渲染（短信/邮箱文案），不经服务端区分。
- `POST /auth/challenge/verify` — body `{identifier, code, remember_device?}`
  - 判别联合信封：
    - `{status:"authenticated", tokens: TokenPairResponse, role}`（tokens 嵌套，对齐 RegisterResponse{user,tokens} 信封先例）
    - `{status:"totp_required", pending_token}`（仅 ADMIN 第一因子通过后）
    - `{status:"unregistered", register_token}`（仅未注册手机号；内联注册三步复用现有 register_token + `/auth/register` 语义）
- `POST /auth/challenge/totp` — body `{pending_token, totp_code, remember_device?}` → authenticated
- `GET /auth/challenge/remembered` — 层 0"一键继续"落点（原 /auth/mobile-code 族内 remembered 腿迁移）：200 `{identifier_masked}`（本机有免码 cookie/凭据；掩码通用标识——有手机用 mask_phone，否则邮箱用 mask_email；受众=空体 verify 腿可服务的全部角色，状态门槛镜像：学生 ACTIVE/PENDING_APPROVAL、员工 ACTIVE。2026-10-20 owner 快轨放开，原为仅学生+`phone_masked`）| 204（无）。只查本机状态，无枚举面。
- `POST /auth/challenge/verify` **空体变体** — 学生免码 cookie 单因子腿（cookie 即因子）；ADMIN 不因 cookie 豁免，仍 `totp_required`。员工免 TOTP 语义只在 cq_staff_device 设备凭据腿（Wave 1 既有语义不变）。

### 3.2 密码登录（既有 `/auth/login` 扩展）

- body `{identifier, password, remember_device?}`；identifier ∈ {手机号, 邮箱, 学号}
- 学生侧先手机后学号（#73 语义并入）；邮箱 → 员工密码流；ADMIN → `totp_required` 腿（复用 `/auth/challenge/totp`）
- 过渡兼容：PR-1 可同时接受旧请求形态；旧形态在退役 PR 移除

### 3.3 退役（独立第三 PR，见 §6 序列）

- `/auth/staff/email-code/*`、`/auth/staff/mobile-code/*`、`/auth/mobile-code/*`（request/verify 两族）、`/auth/staff/login`（密码流并入 `/auth/login`）
- 前端为唯一消费方，无第三方依赖。e2e 探针按**挑战存储**读取（browser_world 直读表），不经端点——退役不破探针；后端集成测试随端点退役重写。
- `/auth/register`、`/auth/password/*`、`/auth/phone/challenges`、邀请接受族：**不动**。

### 3.4 错误码

- 新增 status 成员与错误码走注册表纪律（interfaces.md 先行）。
- 错码/过期/耗尽合一单码 `CHALLENGE_CODE_INVALID`（防枚举：分支差异只入服务端日志）；RATE_LIMITED / ACCOUNT_LOCKED / VALIDATION_ERROR 沿用既有成员。

## 4. 前端设计

单页 `/login`，渐进披露分层：

| 层 | 内容 | 出现条件 |
|---|---|---|
| 0 | "一键继续"条（记住设备/免码 cookie，现有机制按受众） | 本机有凭据 |
| 1 | 「手机号或邮箱」唯一输入框 +「获取验证码」主按钮 +「使用密码登录」弱链接 | 恒显 |
| 1a | 密码框 | 点弱链接展开；此时标识提示三态（手机号/邮箱/学号） |
| 2 | 验证码输入 | 点了获取验证码 |
| 2a | TOTP 输入 | 仅 `totp_required` 后 |
| 2b | 内联注册三步（学号补填） | 仅 `unregistered` 分叉 |

- `/staff/login` → 重定向 `/login`；`StaffInviteAccept` 独立流不动
- 组件复用：`AuthField`/`useCooldown`/`SubmitButton`/`RegisterForm`/TOTP 输入；#113 分支的 ChannelToggle 等共用件延续
- 落点按 role 收口（`landAfterStaffLogin` / 学生仪表盘）
- e2e：六链对统一页重编舞；auth-login 基线随批重拍；staff-login 视觉面退役（页重定向，无独立视觉面）

## 5. 安全边界（评审 blocking 项）

1. **防枚举不新增面**：邮箱恒 202+冷却不变；手机全号真发码（owner 既定决策）；统一响应形态不得引入 oracle（逐分支审查）。
2. **员工邀请制不动**：未注册手机只进学生注册流；员工账号不可能经登录页产生。
3. **TOTP 腿仅第一因子通过后出现**（后验信息，无泄漏）。
4. 已知边缘（记录不阻塞）：员工未绑手机用手机号尝试 → 引向学生注册（第三步学号=身份断言，误触低）；student 行带邮箱 → 恒 202 无码（与"学生无邮箱码"决策一致）。
5. 冷却/锁定/设备凭据语义逐流回归（既有测试面全覆盖，退役 PR 只删不改）。

## 6. 交付编排（三 PR 序列，main 恒绿）

1. **PR-1 后端**：统一挑战端点 + `/auth/login` 扩展（旧端点与旧形态并存保留）+ OpenAPI/TS 再生 + 矩阵行。排位：Wave 2a 收尾后。
2. **PR-2 前端**：统一页 + 六链重编舞 + 基线重拍 + `/staff/login` 重定向。合并门 = PR-1 在 main。
3. **PR-3 后端退役**：§3.3 清单 + 旧请求形态移除 + 契约工件再生。合并门 = PR-2 在 main（前端引用清零）。

双审照走：reviewer 终审即合；部署在 PR-3 后统一收官。
