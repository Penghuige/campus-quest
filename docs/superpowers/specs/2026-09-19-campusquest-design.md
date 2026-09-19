# CampusQuest V1 设计规格

> 状态：Design Approved / Implementation Not Started  
> 日期：2026-09-19  
> 仓库：Penghuige/campus-quest  
> 后端：FastAPI  
> 本文是 CampusQuest V1 的产品与工程实现基准。后续实现 Agent 必须优先遵循本文中的 MUST / SHOULD / MAY 约束；若实现阶段发现本文内部矛盾，应先提出并修正规格，而不是在代码中静默选择一种解释。

## 0. 文档约定

本文使用以下关键词：

- MUST：V1 必须满足，是业务不变量、正确性或安全性要求。
- SHOULD：强烈建议满足；若偏离必须说明原因。
- MAY：允许实现，但不能破坏 MUST 条款。
- “学生”指 Student 角色。
- “教师”指 Teacher 角色。
- “管理员”指 Admin 角色。
- 所有后端时间戳在数据库中 MUST 以 UTC 保存。
- 日榜、月榜、每日放弃次数等“自然日/自然月”规则 MUST 使用统一的 BUSINESS_TIMEZONE 计算。该时区由部署配置提供，不写死在业务代码中。

## 1. 项目目标

CampusQuest 是一个面向本科生的校园众包任务平台。V1 重点服务数据采集类任务，例如小红书、抖音、知乎等平台的关键词数据采集。

核心闭环：

1. 本科生通过学号白名单和唯一手机号注册。
2. 教师创建 Task，并提前导入多个互不重复的 Assignment。
3. 学生领取 Task 时，由系统随机分配一个可用 Assignment，不允许自行挑选关键词。
4. 学生在截止时间前提交 CSV、XLSX 或 SQLite 文件。
5. 系统先进行机器校验，机器校验通过后进入教师人工验收。
6. 人工验收通过后发放积分。
7. 积分可申请兑换课程平时分或无理由请假条等 RewardItem，由人工审核和发放。
8. 系统提供日榜、月榜、总榜、荣誉称号、个人成长页和任务评论区。
9. DDL 前 24 小时、4 小时按 Task 配置的渠道发送提醒。
10. 逾期 24 小时仍无有效提交时释放 Assignment，使其重新进入任务池。

V1 的设计优先级依次是：

1. 业务正确性和可审计性。
2. 并发一致性。
3. 文件解析安全。
4. 权限隔离和隐私保护。
5. 可测试性。
6. 用户体验。
7. 性能优化。

## 2. 非目标

以下内容明确不进入 V1：

- 学生发布任务。
- 微信小程序、原生 iOS/Android App。
- 微服务化。
- 多学校、多租户 SaaS。
- 学生之间积分转账或交易。
- 私聊。
- 任意用户自定义 Python/SQL/JavaScript 校验脚本。
- AI 自动判断提交数据语义质量并直接代替人工验收。
- 复杂任务依赖 DAG。
- 通用规则脚本引擎。
- 依靠客户端直接计算积分、排名或任务状态。

后续 Agent 不应“顺手实现”这些功能。

## 3. 总体架构

采用“模块化单体 FastAPI + 独立异步 Worker”。

    Next.js Web / PWA
             |
             | HTTPS REST
             v
        FastAPI Backend
        ├─ Auth & Identity
        ├─ Task
        ├─ Assignment
        ├─ Submission
        ├─ Points & Reward
        ├─ Ranking & Honor
        ├─ Community
        ├─ Notification
        └─ Admin / RBAC / Audit
             |
      ┌──────┼──────────┐
      v      v          v
    PostgreSQL Redis    S3-compatible Object Storage
             |
             v
        Async Worker
        ├─ 文件解析与校验
        ├─ 通知调度与发送
        ├─ Claim 超时释放
        ├─ 文件生命周期清理
        └─ 排行榜归档与重建

推荐技术栈：

- Frontend：Next.js + TypeScript。
- Backend：FastAPI + Python。
- ORM：SQLAlchemy 2.x。
- Migration：Alembic。
- Database：PostgreSQL。
- Cache / Queue：Redis。
- Worker：Celery 优先；若最终选择 RQ，业务语义不得变化。
- Object Storage：S3-compatible API。
- Testing：pytest + pytest-asyncio；前端使用 Playwright 做 E2E。
- API Schema：FastAPI OpenAPI 自动生成，但业务错误码必须人工定义并稳定。

原则：

- PostgreSQL 是业务事实源。
- Redis 只能做缓存、队列、短期令牌和排行榜投影，不得成为唯一事实源。
- Object Storage 只存文件对象和派生预览，不保存业务状态。
- Worker 不得绕过 Domain/Service 层直接实现一套不同的业务规则。
- API Router 不得直接拼装多张表完成核心状态转换；状态转换必须进入明确的 Service/Domain 方法。

## 4. 角色与权限

### 4.1 Student

可执行：

- 注册、登录、找回密码。
- 修改 nickname、验证/修改手机号、绑定/解绑并验证邮箱。
- 浏览已发布 Task。
- 领取随机 Assignment。
- 主动放弃允许放弃的 Claim。
- 上传提交文件。
- 查看机器校验结果。
- 按要求修改重交。
- 查看自己的积分、排行、荣誉和成长数据。
- 发起 RewardItem 兑换申请。
- 评论、回复、匿名评论、编辑/删除自己的评论。
- 点赞、点踩、Emoji Reaction、举报。
- 在完成 Task 后进行 1–5 星评分。

不得：

- 选择具体 Assignment。
- 修改自己的奖励、DDL、Claim 状态。
- 查看其他学生学号、手机号、邮箱。
- 看到匿名评论的真实身份。

### 4.2 Teacher

除普通社区能力外，可：

- 创建、编辑、发布、暂停、关闭自己负责的 Task。
- 管理自己 Task 的协作者。
- 批量导入 Assignment。
- 查看自己 Task 的参与和提交情况。
- 人工验收 Submission。
- 退回修改。
- 管理自己 Task 下评论和举报。
- 按管理员授权审核相关 RewardRedemption。

教师必须进行资源归属校验。拥有 review_submission 权限并不意味着可审核所有 Task。

教师不应直接看到匿名评论作者的手机号、邮箱。学号等真实身份只有在确有业务需要的后台页面中展示，并遵循最小权限原则。

### 4.3 Admin

拥有全局权限：

- 学生白名单管理。
- Teacher/Admin 账号管理。
- 角色与权限管理。
- 全部 Task、Assignment、Claim 和 Submission 管理。
- RewardItem 管理。
- RewardRedemption 审核与发放。
- 人工积分调整。
- 评论治理。
- 匿名身份追溯。
- 通知模板、Emoji 集合、系统参数管理。
- AuditLog 查询。
- 账号暂停/封禁。
- 重大异常的强制状态修复。

所有敏感操作 MUST 写 AuditLog。

## 5. 身份与账号体系

### 5.1 StudentWhitelist

学生注册前，管理员预先导入本科生学号白名单。

StudentWhitelist 至少包含：

- id
- student_number
- metadata JSONB，可选，用于年级、学院等内部信息
- enabled
- created_at
- disabled_at

要求：

- student_number MUST 全局唯一。
- 导入白名单不等于创建 User。
- 禁用白名单不自动删除已经注册的账号；若需要禁用账号，必须单独修改 User 状态。

### 5.2 username / 学号

Student 的 username 就是学号。

规则：

- MUST 只接受 ASCII 数字 0–9。
- 不允许空格、短横线、小数点、全角数字、科学计数法形式。
- 长度不固定。实现 SHOULD 采用可配置 min/max，默认 6–20 位，避免无界输入。
- 注册时 MUST 同时通过格式校验和 StudentWhitelist 命中校验。
- username MUST 全局唯一。
- 注册后 Student 不能自行修改。
- 登录时可以去除首尾空白，但不能改写内部字符。
- 不允许把学号转 integer 保存，MUST 以 string 保存，避免前导 0 丢失。

边界测试：

- 20250010001：接受。
- 000123456：若白名单存在则接受，前导 0 必须保留。
- ２０２５００１：拒绝。
- 2025 001：拒绝。
- 2025-001：拒绝。
- 空字符串：拒绝。
- 超出配置长度：拒绝。

### 5.3 nickname

规则：

- 最大长度 16 个“用户感知字符”，SHOULD 按 Unicode grapheme cluster 计数，而不是 UTF-8 字节数。
- 字符种类不限制，可包含中文、英文、数字、空格和 emoji。
- 必须去除不可见控制字符。
- 前后空白 SHOULD trim。
- trim 后不能为空。
- 输出到 HTML 时必须依赖框架默认转义，禁止 raw HTML。
- nickname 不要求唯一。

测试必须覆盖组合 emoji、ZWJ emoji、中文、英文和混合字符串，避免按字节截断导致乱码。

### 5.4 手机号

Student 强制绑定手机号。

规则：

- MUST 短信验证码验证。
- 一个规范化后的手机号全系统只能绑定一个账号。
- SHOULD 使用标准号码解析库并以 E.164 形式保存。
- 原始输入只用于本次解析，不作为唯一性比较依据。
- 修改手机号必须：已登录 + 再验证身份 + 新手机号 OTP 验证。
- 新手机号必须先做唯一性检查，最终唯一性仍由数据库 UNIQUE 保证。
- 日志中手机号必须脱敏。
- 不允许依赖“先查不存在，再插入”保证唯一性；并发注册最终必须由数据库约束兜底。

### 5.5 邮箱

- 可选。
- 只有 verified email 才能接收正式业务邮件。
- email SHOULD 大小写规范化后做唯一性约束；具体是否允许同邮箱多账号可配置，V1 推荐全局唯一。
- 未验证邮箱存在时，EMAIL 通知应跳过而不是标记系统故障。
- 用户可解绑邮箱，解绑前不得影响手机号登录能力。

### 5.6 密码与会话

学生登录：学号 + 密码。

密码：

- MUST 使用 Argon2id。
- 不保存明文或可逆密文。
- 建议长度 10–128。
- 不强制无意义的大小写/特殊字符组合规则。
- 修改密码、找回密码后 SHOULD 使旧 Refresh Session 失效。

会话：

- Access Token 短期有效。
- Refresh Token 可撤销、可轮换。
- Refresh Token 对应服务端 Session 记录。
- Web 端不得把长期 Refresh Token 放 localStorage。
- 推荐 HttpOnly + Secure + SameSite Cookie。
- 若使用 Cookie 认证，所有有副作用请求必须有 CSRF 防护。

Teacher/Admin：

- MUST 强制 2FA。
- 推荐 TOTP + 一次性恢复码。
- 后台支持额外 IP/VPN 白名单策略，但 IP 白名单不能替代正常认证和 2FA。

### 5.7 User 状态

    PENDING_PHONE -> ACTIVE
    ACTIVE -> SUSPENDED
    ACTIVE -> BANNED
    SUSPENDED -> ACTIVE
    BANNED -> ACTIVE 仅 Admin 明确解除

SUSPENDED/BANNED 用户：

- 不能领取新任务。
- 不能提交新文件。
- 不能新增社区内容。
- 已有积分和审计数据保留。
- 不做物理删除以破坏历史引用。

### 5.8 Teacher / Admin 账号建立

Teacher/Admin 不走 StudentWhitelist 自助注册。

V1 推荐：

1. Admin 在后台创建 Staff 邀请。
2. Staff 通过一次性、短时有效邀请链接设置密码。
3. 首次登录必须绑定并启用 TOTP 2FA，未完成前不能进入管理后台。
4. Recovery Code 只在启用 2FA 时展示一次，服务端仅保存 hash。
5. Admin 账号的创建、角色提升、角色撤销都必须写 AuditLog。

Staff 的登录标识可以使用独立 username 或 verified email，但不得假冒 Student 学号身份。实现计划必须统一一种 Staff 登录方式，不能让不同页面各自解释。

## 6. Task

Task 表示“一个可由多人参与的任务”。

主要字段建议：

- id
- owner_teacher_id
- title
- description
- task_type，V1 至少 DATA_CRAWL
- rarity：NORMAL / RARE / EPIC / LEGENDARY
- base_reward_points
- status
- deadline_mode：FIXED / RELATIVE
- fixed_deadline_at，可空
- duration_minutes，可空
- grace_period_minutes，V1 固定为 1440（24 小时）；V1 不提供按 Task 修改宽限期的产品入口
- claim_cutoff_minutes，FIXED 模式默认 240
- submission_schema JSONB
- allowed_file_types
- max_file_size_bytes
- notify_24h
- notify_4h
- notification_channels
- retention_policy
- published_at
- closed_at
- created_at / updated_at

### 6.1 rarity

NORMAL / RARE / EPIC / LEGENDARY 只作为视觉和氛围标签。

MUST NOT：

- 自动决定奖励倍率。
- 自动决定 DDL。
- 自动决定参与人数。
- 自动改变权限。

上述业务参数全部由 Task 独立配置。

### 6.2 Task 状态机

    DRAFT -> PUBLISHED
    PUBLISHED -> PAUSED
    PAUSED -> PUBLISHED
    PUBLISHED/PAUSED -> CLOSED
    CLOSED -> ARCHIVED

语义：

- DRAFT：学生不可见。
- PUBLISHED：学生可见，可领取。
- PAUSED：已有 Claim 继续有效，停止新领取。
- CLOSED：不允许新领取；已有 Claim 如何处理由关闭动作明确选择，默认继续有效。
- ARCHIVED：仅历史查询。

禁止：

- Teacher 通过“暂停”偷偷取消已有 Claim。
- 修改已发布 Task 的 base_reward_points 并追溯影响已领取 Claim。

关键快照原则：

当 Claim 创建时，MUST 快照至少以下字段：

- base_reward_points
- deadline_at
- grace_deadline_at
- 奖励阶梯版本或 reward_policy_snapshot
- submission_schema_version

之后 Task 配置变化不得静默改变已有 Claim 的核心合同。

## 7. Assignment

Task 下预先存在多个 Assignment。学生不能自行输入或选择 Assignment。

V1 数据爬取 Assignment 至少包含：

- id
- task_id
- platform
- keyword
- payload JSONB，可选扩展字段
- availability_status
- created_at

availability_status 至少定义：

- AVAILABLE：可被随机领取。
- OCCUPIED：当前存在 active Claim。
- COMPLETED：已经有成功完成的 Claim，永久不再分配。
- RETIRED：管理员主动下架该 Assignment，不参与分配。

ABANDONED / EXPIRED 是 Claim 的终态，不是 Assignment 的永久状态；发生后 Assignment 通常回到 AVAILABLE。

数据库必须保证：

    UNIQUE(task_id, platform, keyword)

比较规则必须明确：

- platform SHOULD 使用受控枚举/规范化值，例如 xiaohongshu、douyin、zhihu，而不是自由中文字符串。
- keyword 默认按 trim 后原始 Unicode 文本比较。
- 是否大小写敏感需在 Task 创建时固定；中文场景默认按精确文本。
- 禁止仅靠应用层查重。

如果未来 Assignment 不止 platform + keyword，允许升级为 canonical assignment key，但 V1 不应为了未来场景引入过度复杂的通用 DSL。

### 7.1 批量导入

Teacher 可 CSV/XLSX 导入。

流程 MUST 是：

1. 上传。
2. 解析。
3. 预检查。
4. 展示有效条数、错误条数、逐行错误。
5. 用户明确确认。
6. 单事务或分批可恢复地正式写入。

需要检测：

- 空 platform。
- 空 keyword。
- 不支持 platform。
- 文件内部重复组合。
- 与数据库现有 Assignment 重复。
- 超长文本。
- 非法编码。
- 超过单次导入上限。

正式写入时仍依赖数据库 UNIQUE 兜底，防止预览和确认之间其他人并发导入。

## 8. AssignmentClaim

Assignment 是可复用工作单元；AssignmentClaim 是一次领取历史。不得通过覆盖 Assignment 的 owner 字段丢失历史。

建议字段：

- id
- assignment_id
- task_id
- user_id
- status
- claimed_at
- deadline_at
- grace_deadline_at
- reward_policy_snapshot
- base_reward_points_snapshot
- reward_lock_status
- reward_tier_locked
- reward_locked_at
- locked_reward_points
- latest_submission_id
- revision_deadline_at
- terminal_at

### 8.1 Claim 状态

建议至少：

- CLAIMED
- VALIDATING
- UNDER_REVIEW
- REVISION_REQUIRED
- COMPLETED
- ABANDONED
- EXPIRED

若实现需要 SUBMITTED/VALIDATED 作为显式状态可以增加，但不得造成多套相互矛盾的“Submission 状态”和“Claim 状态”。

状态转换必须由服务层集中定义。

### 8.2 领取限制

学生领取前 MUST 检查：

- User ACTIVE。
- Task PUBLISHED。
- 未超过全局“当前可操作 Claim”上限。
- 同一 Task 没有冲突中的 Claim。
- 有 AVAILABLE Assignment。
- FIXED DDL 模式未到 claim cutoff。
- 该用户没有命中该 Task 的额外限制。

全局并行限制：

- 每名学生最多同时持有 3 个“仍需要学生行动”的 Claim。
- CLAIMED 和 REVISION_REQUIRED 必须计入。
- 文件已经成功提交且正在 VALIDATING / UNDER_REVIEW 时，不应占用这 3 个名额，因为学生无法影响审核速度。
- 同一 Task 同一时刻最多一个非终态 Claim。

完成、放弃或终止后是否允许再次领取同一个 Task：
- V1 允许再次领取，只要不存在同 Task 非终态 Claim。
- 但同一个 Assignment 不得重复分配给曾经 ABANDONED 或 EXPIRED 它的同一用户。
- 已 COMPLETED 的 Assignment 永久完成，不重新进入 AVAILABLE。

### 8.3 并发随机领取

领取必须在数据库事务中完成。

PostgreSQL 推荐逻辑：

    BEGIN
    锁/校验用户领取额度
    SELECT candidate assignment
      WHERE task_id = ...
        AND availability_status = AVAILABLE
        AND assignment_id NOT IN 当前用户禁止再次领取集合
      ORDER BY 随机策略
      FOR UPDATE SKIP LOCKED
      LIMIT 1
    创建 AssignmentClaim
    将 Assignment 标记 OCCUPIED
    COMMIT

不能采用：

    SELECT 一个随机 Assignment
    然后在事务外 UPDATE

因为会重复分配。

随机策略 MAY 使用预生成 random_key 或其他更高效方案，但必须保持“无法由用户选择具体 Assignment”的产品语义。

数据库必须保证“一个 Assignment 同一时刻最多一个 active Claim”。推荐部分唯一索引或清晰的 availability_status + 行锁不变量。

同一用户连续或并发领取时，还必须防止“3 个上限”被穿透。仅在两个事务中分别 COUNT 当前 Claim 然后 INSERT 是不安全的。claim_random_assignment MUST 在检查用户配额前锁定一个稳定的用户级资源，例如 User 行、专用 ClaimQuota 行或 PostgreSQL advisory lock；同一用户的领取事务必须串行化后再检查计数。

同一用户同一 Task 的非终态 Claim 约束 SHOULD 再由 PostgreSQL partial unique index 兜底；若最终状态模型不适合 partial index，必须提供同等强度的数据库/锁级保证并写并发集成测试。

### 8.4 领取失败错误

至少定义：

- TASK_NOT_CLAIMABLE
- ASSIGNMENT_LIMIT_REACHED
- TASK_ACTIVE_CLAIM_EXISTS
- NO_ASSIGNMENT_AVAILABLE
- CLAIM_CUTOFF_REACHED
- ACCOUNT_NOT_ACTIVE

这些错误返回 4xx，不得 500。

### 8.5 主动放弃

- 每个 BUSINESS_TIMEZONE 自然日最多 2 次。
- 上限 SHOULD 可配置。
- 放弃不扣已有积分。
- 放弃写行为历史。
- Claim -> ABANDONED。
- Assignment -> AVAILABLE。
- 当前用户后续不得重新随机到同一个 Assignment。

每日次数的并发检查必须原子化；两个并发放弃请求不得使限制被绕过。

重复调用同一个 abandon API 应幂等：第一次成功，后续返回“已经终态”或相同结果，不重复增加 abandon_count。

## 9. Deadline

### 9.1 FIXED

所有 Claim 共享 Task.fixed_deadline_at。

领取时：

    deadline_at = Task.fixed_deadline_at
    grace_deadline_at = deadline_at + 24h

默认 claim cutoff：

- 距 deadline 少于 4 小时时停止新领取。
- cutoff 可配置。
- 若 Task 本身发布时离 deadline 已不足 cutoff，系统应明确提示并禁止领取，而不是生成“负剩余时间”提醒。

### 9.2 RELATIVE

领取时：

    deadline_at = claimed_at + duration
    grace_deadline_at = deadline_at + 24h

之后 Teacher 修改 duration 不影响已存在 Claim。

### 9.3 时间边界

所有比较使用 UTC instant。

V1 奖励分段定义为：

- submitted_at <= deadline_at：100%
- deadline_at < submitted_at < deadline_at + 4h：80%
- deadline_at + 4h <= submitted_at < deadline_at + 12h：50%
- deadline_at + 12h <= submitted_at < grace_deadline_at：20%
- submitted_at >= grace_deadline_at：禁止作为新有效提交

因此：

- 恰好 deadline：100%。
- 恰好 deadline + 4h：50%。
- 恰好 deadline + 12h：20%。
- 恰好 deadline + 24h：不再接受。

这些边界 MUST 有单元测试，避免前后端各自使用不同的 < / <=。

## 10. 文件上传

支持：

- CSV
- XLSX
- SQLite / DB，仅当确认为 SQLite

Task 可限制允许格式。

上传采用 presigned URL：

1. Student 请求 upload intent。
2. Backend 校验 Claim 权限、状态、文件类型声明、大小上限和提交窗口。
3. Backend 生成短时 presigned URL / POST。
4. 浏览器直接上传 Object Storage。
5. 客户端通知 Backend 上传完成。
6. Backend 验证对象存在、实际 size 等元数据。
7. 创建 Submission。
8. Worker 异步验证。

安全要求：

- Object key 由服务端生成，不使用原文件名作为路径。
- 原文件名仅做显示 metadata，并进行长度限制和清理。
- 下载使用短时签名 URL。
- 不允许用户构造任意 object_key 读取他人文件。
- 文件上限默认 200 MB，可按 Task 调整。
- 解析器还需要独立的行数、列数、解压大小、CPU、内存和超时限制；不能认为 200 MB 原文件就是唯一资源上限。

## 11. Submission

一次 Claim 可有多个 Submission version。

字段建议：

- id
- claim_id
- version
- object_key
- original_filename
- declared_type
- detected_type
- file_size
- submitted_at
- validation_status
- review_status
- validation_report JSONB
- reviewer_id
- reviewed_at
- review_note
- created_at

版本 MUST 单调递增并在 Claim 内唯一。

### 11.1 Submission 状态

机器阶段：

    UPLOADED -> VALIDATING -> VALIDATED
                         -> VALIDATION_FAILED

人工阶段：

    VALIDATED -> UNDER_REVIEW
              -> APPROVED
              -> REVISION_REQUIRED

APPROVED 后 Claim -> COMPLETED。

### 11.2 首次有效提交与奖励锁

“有效提交”定义为机器校验成功的 Submission。

奖励档位的时间使用该 Submission.submitted_at，而不是：

- Worker 实际开始解析时间。
- Worker 验证完成时间。
- Teacher 打开审核页时间。
- Teacher 点击通过时间。

第一次机器校验通过后：

- reward_lock_status = PROVISIONAL
- reward_tier_locked = 按 submitted_at 计算
- reward_locked_at = submitted_at
- locked_reward_points = 按 Claim 的 base_reward_points_snapshot 计算

reward_lock_status V1 明确定义为：

- NONE：尚无机器校验通过的有效提交。
- PROVISIONAL：已按首次有效提交锁档，等待人工确认。
- CONFIRMED：人工验收通过，已作为最终奖励依据。
- INVALIDATED：上一份 provisional lock 因明确恶意/空壳被人工判无效；后续新有效提交可以建立新的 PROVISIONAL lock。

实现可以通过独立 RewardLock 历史表保留多次 lock 变化；无论采用哪种表结构，都不得覆盖掉 INVALIDATED 的审计历史。

普通质量问题导致 REVISION_REQUIRED 时，保留该奖励档位。

### 11.3 恶意空壳锁奖励

人工审核可区分：

- 普通可修正质量问题：REVISION_REQUIRED，保留奖励锁。
- 明显空壳、伪造、故意绕过机器校验：INVALIDATE_REWARD_LOCK。

INVALIDATE_REWARD_LOCK：

- 必须要求 reviewer reason。
- 写 AuditLog。
- 取消当前 provisional lock。
- 后续新的有效 Submission 按新的 submitted_at 重新锁档。
- 不应自动扣用户历史积分；若已经错误发放则通过反向 PointsLedger 冲销。

### 11.4 Revision window

当 Teacher 在较晚时间退回时，不应让学生因为审核延迟失去修改机会。

    revision_deadline_at =
      max(original grace_deadline_at, reviewed_at + 24h)

若再次退回，则基于新的 reviewed_at 再计算新的 revision_deadline_at。

奖励档位仍保持首个未被判无效的有效提交。

学生若在 revision_deadline_at 前未重交：

- Claim -> EXPIRED。
- 若 Assignment 尚未完成，应释放为 AVAILABLE。
- 该 Claim 不发奖励。
- 历史 Submission 保留。

### 11.5 DDL 与审核

核心原则：

“DDL 约束学生提交行为，不约束教师审核耗时。”

只要在允许提交窗口内产生机器校验通过的 Submission，Claim 就不得因为 Teacher 审核跨过 grace_deadline_at 而被后台超时任务释放。

## 12. Submission Schema

V1 使用有限、可解释的 JSON Schema DSL，而不是任意代码。

至少支持：

- allowed_formats
- source_selector
- min_rows
- max_rows
- required_columns
- optional_columns
- allow_extra_columns
- column type
- nullable
- unique
- max_null_ratio，可选

字段类型至少：

- string
- integer
- number
- boolean
- datetime

示例语义：

    required column url: string, unique
    required column title: string
    required column publish_time: datetime
    optional column likes: integer
    min_rows: 500

### 12.1 CSV

V1 SHOULD 明确支持 UTF-8 和 UTF-8 BOM。

对于非 UTF-8 编码：

- 可选择明确拒绝并提示用户转换编码。
- 若实现自动 GB18030 fallback，必须有明确测试，不得静默错误解码。

必须限制：

- 最大行数。
- 最大列数。
- 单元格最大长度。
- 解析时间。
- CSV dialect 侦测失败的清晰错误。

### 12.2 XLSX

使用只读流式解析。

必须防止：

- zip bomb / 极高解压比。
- 极端 sharedStrings。
- 百万空行。
- 巨量列。
- 外部链接。
- 公式内容被当作可信执行结果。

推荐：

- 默认读取 Task 指定 sheet。
- 未指定时读取第一张可见 worksheet。
- 找不到 sheet -> 校验失败。
- 对公式单元格不执行公式。
- 预览/重新导出到 CSV/XLSX 时需要防 Spreadsheet Formula Injection。

### 12.3 SQLite

只允许真正的 SQLite 3 文件。

至少：

- 检查 header。
- 只读打开。
- query_only。
- 禁止 extension loading。
- 禁止 ATTACH。
- 不执行用户提供 SQL。
- 只执行服务端生成的、严格引用标识符的 SELECT / PRAGMA。
- Parser 运行在受资源限制的 Worker。
- 设置 busy timeout 和总处理超时。
- 不允许将上传文件作为应用主数据库挂载。

表选择：

- Task 可指定 table_name。
- 若未指定且恰好一个用户表，可选该表。
- 若多个用户表且未指定，机器校验失败并明确提示。
- sqlite 系统表不算用户表。

### 12.4 自动校验报告

应结构化保存，而不是只存一段字符串。

示例字段：

- parser_version
- file_type
- row_count
- detected_columns
- missing_required_columns
- extra_columns
- type_error_counts
- null_ratios
- duplicate_counts
- warnings
- errors
- duration_ms

人工审核 UI 显示：

- Assignment platform / keyword。
- 提交版本。
- submitted_at。
- 奖励锁档。
- 自动校验摘要。
- 前 N 行安全预览。
- 下载链接。
- 历史版本。
- 通过 / 退回。
- 审核备注。

## 13. 文件保留

每个 Task 可配置原始上传文件保留：

- 30 天
- 90 天
- 180 天，默认
- 永久

实现 SHOULD 存 retention_until 快照，避免以后 Task 改策略导致旧文件含义不清。

清理 Worker：

- 只删除已到期、且没有 legal hold 的对象。
- 删除 Object Storage 后更新数据库状态。
- 删除失败可重试。
- 数据库业务元数据、Submission 校验报告和审计记录不随原文件删除。
- 永久保留必须显式标记，不使用“非常大的日期”模拟。

## 14. 人工审核与积分发放事务

Teacher APPROVE 一个 Submission 时，必须在一个数据库事务中：

1. 锁 Claim。
2. 验证 Claim 仍可审核。
3. 验证 Submission 属于该 Claim 且是当前有效版本。
4. 验证 reviewer 对 Task 有权限。
5. 将 Submission -> APPROVED。
6. Claim -> COMPLETED。
7. reward_lock_status -> CONFIRMED。
8. 创建唯一 PointsLedger ASSIGNMENT_REWARD。
9. Assignment -> COMPLETED/不可再分配。
10. 写必要审计记录。

数据库必须防止重复发分。

推荐唯一约束：

    UNIQUE(source_type, source_id, ledger_type)

其中一个 Claim 的 ASSIGNMENT_REWARD 最多一条有效原始奖励记录。

两个 Teacher 并发点击“通过”时：

- 只能一个事务真正创建奖励。
- 第二个得到幂等成功或 ALREADY_REVIEWED。
- 绝不能发两次积分。

## 15. Points Ledger

积分禁止通过直接修改 users.points 实现。

PointsLedger 至少包含：

- id
- user_id
- ledger_type
- amount，signed integer
- source_type
- source_id
- affects_balance
- affects_ranking
- ranking_effective_at
- reversal_of_id，可空
- operator_id，可空
- reason，可空
- created_at

典型类型：

- ASSIGNMENT_REWARD
- ASSIGNMENT_REWARD_REVERSAL
- REWARD_REDEMPTION
- REWARD_REDEMPTION_REFUND
- ADMIN_ADJUSTMENT

原则：

- 原始 Ledger 行不可 UPDATE 金额、不可 DELETE。
- 修正通过新增反向流水完成。
- Admin 调整必须有 reason。
- Admin Adjustment 默认 affects_ranking = false。

### 15.1 available_points 与 earned_points

概念分离：

- available_points：可消费余额。
- earned_points：累计任务贡献，用于总榜和长期荣誉。

兑换不应该降低 earned_points 和历史排名。

实现 MAY 有 PointWallet / PointBalance 投影表以提高查询和并发控制，但：

- Ledger 是可审计事实源。
- 投影必须可从 Ledger 重建。
- Ledger 与投影更新必须在同一事务。

## 16. RewardItem 与兑换

RewardItem 字段建议：

- id
- name
- description
- point_cost
- stock，可空表示不限
- per_user_term_limit
- available_from / available_until
- enabled
- requires_manual_review
- fulfillment_instructions
- created_at / updated_at

典型：

- 平时成绩 +1
- 无理由请假条 x1

具体价格、库存、学期上限全部由 Admin 配置，不能写死在代码中。

### 16.1 Redemption 状态

    REQUESTED -> UNDER_REVIEW -> APPROVED -> FULFILLED
                              -> REJECTED

也可在创建后直接进入 UNDER_REVIEW。

“每学期每人上限”不能用自然半年猜测。V1 使用管理员配置的 `CURRENT_ACADEMIC_TERM` 字符串作为学期键，例如 `2026-fall`。创建 RewardRedemption 时 MUST 将当前 term key 快照到 Redemption；同一 RewardItem 的 per-user-term-limit 按该快照键统计。管理员切换当前 term 只影响之后的新申请，不改写历史 Redemption。

申请时：

- 检查 available_points。
- 检查 RewardItem enabled。
- 检查时间窗口。
- 检查库存。
- 检查每人学期上限。
- 原子冻结积分。
- 对有限库存 RewardItem 同时原子预占 1 个库存名额。

库存语义必须明确：

- REQUESTED / UNDER_REVIEW / APPROVED 但未终止的兑换占用库存。
- REJECTED 或明确取消后释放库存预占。
- FULFILLED 将预占转为永久消耗。
- 对 RewardItem 行或库存账户做数据库锁，不能用“先查 stock > 0 再异步减 1”的方式。

RewardItem 可兑换时间窗口在 V1 统一采用半开区间：`available_from <= now < available_until`；任一端为 null 表示该方向无界。每学期每人上限统计同一 `reward_item_id + term_key` 下仍占用资格或已批准的申请（REQUESTED / UNDER_REVIEW / APPROVED / FULFILLED）；REJECTED 不占用学期限额。有限库存同理，REJECTED 释放预占。

### 16.2 积分冻结

推荐 PointReservation / Redemption reservation 语义。

申请成功：

    spendable = ledger_balance - active_reservations

审核通过：

- 释放 reservation。
- 写 REWARD_REDEMPTION 负流水。
- Redemption -> APPROVED。

审核拒绝：

- 释放 reservation。
- 不产生消费负流水。
- Redemption -> REJECTED。

APPROVED 和 FULFILLED 分离，因为“批准兑换”和“实际录入平时分/发放请假条”可能不同时间完成。

### 16.3 并发兑换

必须测试：

- 用户只有 1500 分，同时请求两个 1000 分 RewardItem。
- 最终最多一个申请成功冻结。
- 不能出现 available_points = -500。

实现推荐锁 PointWallet 行或使用等价强一致事务策略。

库存同样必须防 oversell。

## 17. 排行榜

提供：

- 日榜。
- 月榜。
- 总榜。
- 我的附近。

只展示：

- nickname
- display_honor
- ranking score
- rank

绝不展示：

- 学号
- 手机号
- 邮箱

### 17.1 排名积分口径

日榜、月榜、总榜基于有效任务贡献，不基于可消费余额。

ASSIGNMENT_REWARD：

- affects_balance = true
- affects_ranking = true

REWARD_REDEMPTION：

- affects_balance = true
- affects_ranking = false

ADMIN_ADJUSTMENT 默认：

- affects_balance = true
- affects_ranking = false

### 17.2 冲销

若历史任务奖励被确认作弊：

- 不删除原 reward ledger。
- 新增 ASSIGNMENT_REWARD_REVERSAL。
- affects_balance 与 ranking 应按业务决定，正常作弊冲销两者都影响。
- reversal 必须链接原 ledger。

排名周期归属：

- reversal 的 ranking_effective_at SHOULD 使用原奖励的 ranking_effective_at。
- 例如 8 月奖励在 9 月被冲销，应修正 8 月历史榜和总榜，而不是把 -200 算到 9 月学生表现。

此规则必须有测试。

### 17.3 Redis

Redis Sorted Set 可维护：

- ranking:daily:<date>
- ranking:monthly:<month>
- ranking:all

但 Redis 是投影。

必须提供从 PostgreSQL 重建排行榜的方法。

积分事务提交成功后再更新缓存；缓存更新失败不得回滚已经合法完成的任务，后续由重建/事件重放修复。

## 18. 荣誉

荣誉与积分资产分离。

自动荣誉类型第一版固定支持：

- TOTAL_COMPLETED
- ON_TIME_STREAK
- DAILY_RANK
- MONTHLY_RANK
- TOTAL_EARNED_POINTS

示例：

- 首次完成任务。
- 累计完成 10 个。
- 累计完成 50 个。
- 连续 10 个任务按时。
- 今日卷王。
- 本月卷王。
- 月度 Top 3。

用户可拥有多个 Honor，但只能设置一个 display_honor_id。

周期性荣誉必须带 period，例如 2026-09 月度第一，而不是无时间语义的永久“本月卷王”。

Admin 可以人工创建纪念 Honor，但不允许该操作自动篡改排行榜积分。

## 19. 个人成长页

至少展示：

- 本月获得积分。
- 本月排名及变化。
- 总 earned points。
- 累计完成任务数。
- 按时完成率。
- 当前连续按时数。
- 最佳历史月排名。
- 已获得荣誉。

按时完成定义：

- 首个最终有效 reward lock 的 submitted_at <= deadline_at。
- 之后 revision 不改变这一“是否按时”判定。
- 被 INVALIDATE_REWARD_LOCK 的空壳提交不能让任务被视为按时。

## 20. Task 评分

TaskRating：

- task_id
- user_id
- rating 1–5
- created_at
- updated_at

约束：

    UNIQUE(task_id, user_id)
    CHECK(rating >= 1 AND rating <= 5)

只有至少完成过该 Task 一个 Claim 的用户可评分。

评分允许修改，不允许创建多条。

前台只展示聚合分和数量，不公开“哪个学生给几星”。

## 21. 评论

Comment 字段：

- id
- task_id
- user_id
- parent_id，可空
- content
- is_anonymous
- created_at
- updated_at
- deleted_at
- deleted_by
- delete_reason

### 21.1 发布

- 无需预审，发布即展示。
- 单条长度默认上限 2000 个字符，可配置。
- 防 XSS。
- 去除危险控制字符。
- rate limit。
- 空白评论拒绝。
- 用户可以公开 nickname 或选择本条匿名。

匿名仅是这一条评论的展示属性，不改变用户排行榜身份。

### 21.2 回复

数据库 parent_id 可表达任意深度。

前端 SHOULD 采用两层视觉结构，后续深层回复归入根线程，避免无限缩进。

必须防：

- parent_id 指向另一个 Task 的 Comment。
- 自己构造循环引用。
- 回复不存在或彻底隐藏评论。
- 越权修改别人评论。

### 21.3 编辑和删除

用户可编辑/删除自己的评论。

编辑：

- 显示“已编辑”。
- 后台保存修改历史，建议 CommentRevision。
- 普通用户只看到最新版。

删除：

- 默认软删除。
- 父评论删除后子评论保留。
- 前台显示“该评论已删除”。
- Admin 在特殊隐私/违法场景可彻底隐藏内容，但 AuditLog 仍保留操作记录。

### 21.4 匿名

普通用户：

- 只看到“匿名用户”。

Teacher：

- 可治理自己 Task 评论。
- 在“匿名评论治理上下文”中不得显示 student_number、手机号、邮箱、登录 username 等可直接识别信息。
- 如治理实现确实需要稳定关联，可显示专用于治理的内部 pseudonymous moderation key；该 key 不得在学生端出现，也不得等同于学号。
- Teacher 在 Submission/Claim 等非匿名业务页面是否能查看学生学号，由教学业务权限决定；这不能反向用于匿名评论页面去身份化。

Admin：

- 可以通过专门的“揭示匿名身份”操作追溯真实账号。
- 该操作必须要求明确权限和 reason。
- 每次匿名身份追溯 MUST 写 AuditLog；不能因为 Admin 打开普通评论列表就自动把所有匿名作者展开。

## 22. Vote 与 Emoji Reaction

CommentVote：

- comment_id
- user_id
- value：+1 或 -1

约束：

    UNIQUE(comment_id, user_id)

允许：

- none -> like
- like -> none
- like -> dislike
- dislike -> like

切换 MUST 原子化。

CommentReaction：

- comment_id
- user_id
- emoji

约束：

    UNIQUE(comment_id, user_id, emoji)

V1 emoji 从 Admin 配置白名单选择，默认可包含：

- 👍
- ❤️
- 😂
- 🎉
- 😭
- 👀
- 🤔
- 🔥

不允许用户提交任意 HTML 或图片 reaction。

## 23. 举报

CommentReport 至少：

- id
- comment_id
- reporter_user_id
- category
- note
- status
- handled_by
- handled_at

类别：

- SPAM
- HARASSMENT
- PRIVACY
- OTHER

举报：

- 不自动删除评论。
- 进入治理队列。
- 同一用户对同一评论同一类别 SHOULD 防止重复刷举报。
- 被举报用户不可看到举报者身份。

## 24. 评论排序

V1 支持：

- 最新。
- 最热。

最热算法属于可替换实现细节，不是业务不变量。

第一版可采用简单时间衰减分数，至少考虑：

- likes
- dislikes
- reactions
- age

不得将客户端传入的 hot_score 当事实值。

## 25. 通知系统

业务逻辑生成事件，Notification 模块负责投递。

事件至少：

- ASSIGNMENT_DEADLINE_24H
- ASSIGNMENT_DEADLINE_4H
- REVISION_REQUIRED
- SUBMISSION_APPROVED
- REWARD_REDEMPTION_APPROVED
- REWARD_REDEMPTION_REJECTED
- ACCOUNT_SECURITY

渠道：

- SMS
- EMAIL
- IN_APP

虽然原始需求重点是短信和邮件，V1 SHOULD 提供轻量站内通知作为兜底。

### 25.1 Task 通知策略

每个 Task 可配置：

- notify_24h
- notify_4h
- SMS on/off
- EMAIL on/off
- IN_APP on/off

默认：

- SMS on。
- EMAIL 对 verified email on。
- IN_APP on。

Teacher/Admin 可按 Task 关闭某一业务渠道。

### 25.2 DDL reminder 边界

若领取时距 deadline：

- >24h：计划 24h 与 4h。
- 4h–24h：不补发 24h，只计划 4h。
- <4h：不补发过去的提醒；领取成功页面直接提示剩余时间。
- <=0：禁止领取。

学生进入已经成功提交、VALIDATING 或 UNDER_REVIEW 状态后，未发送的普通 DDL 提醒必须取消/跳过。

如果机器校验失败且 Claim 回到需要学生操作状态，则重新判断尚未错过的未来提醒。

### 25.3 NotificationDelivery

至少：

- id
- event_key
- user_id
- channel
- status
- scheduled_at
- attempts
- sent_at
- provider_message_id
- last_error

唯一约束：

    UNIQUE(event_key, user_id, channel)

例：

    claim:123:deadline_4h

Celery 重试不得造成双发。

### 25.4 重试

建议：

- 第 1 次失败后约 1 分钟。
- 第 2 次约 5 分钟。
- 第 3 次约 20 分钟。
- 最多 3 次或配置值。

永久失败：

- status = FAILED。
- 后台可查询失败原因。
- 站内通知若可用仍保留。

通知发送失败不得回滚任务状态或积分状态。

### 25.5 Template

NotificationTemplate：

- event_type
- channel
- title
- template_body
- enabled
- version

Admin 修改模板。

Teacher 不允许任意修改全局短信模板。

模板渲染必须使用受限变量，不执行代码。

## 26. Claim 超时 Worker

定时任务扫描满足：

- Claim 仍处于需要学生提交的状态。
- 当前时间 >= grace_deadline_at 或 revision_deadline_at。
- 没有已经锁定并在审核流程中的有效 Submission。

则：

1. 锁 Claim。
2. 再次检查状态，防止扫描后到执行前学生刚好提交。
3. Claim -> EXPIRED。
4. Assignment -> AVAILABLE。
5. 写必要事件/审计。

必须防止竞态：

- 23:59:59.900 学生创建有效提交。
- 24:00:00.000 超时 Worker 同时扫描。

最终只允许一个合法结果。推荐锁 Claim 并以数据库保存的 submitted_at / Submission 状态做事务内复检。

## 27. 文件清理 Worker

清理任务必须幂等。

对象已不存在时：

- 若数据库已标记删除，则视为成功。
- 若数据库认为存在但对象 404，应记录 reconcile warning，并修正状态。

不得误删：

- 尚在审核中的文件。
- retention_until 未到文件。
- permanent 文件。
- 被 legal_hold 标记文件。

## 28. API 原则

统一前缀：

    /api/v1

示例：

    POST /auth/register
    POST /auth/login
    POST /auth/refresh
    POST /auth/password/forgot

    GET  /tasks
    GET  /tasks/{task_id}
    POST /tasks/{task_id}/claim

    POST /claims/{claim_id}/abandon

    POST /submissions/upload-intent
    POST /submissions/{submission_id}/upload-complete
    GET  /submissions/{submission_id}/validation

    POST /teacher/tasks
    POST /teacher/tasks/{task_id}/assignments/import/preview
    POST /teacher/tasks/{task_id}/assignments/import/confirm
    GET  /teacher/submissions/review-queue
    POST /teacher/submissions/{submission_id}/approve
    POST /teacher/submissions/{submission_id}/revision-required

    GET  /points/me
    GET  /rankings/daily
    GET  /rankings/monthly
    GET  /rankings/all

    GET  /rewards
    POST /rewards/{reward_id}/redeem

    POST /tasks/{task_id}/comments
    PATCH /comments/{comment_id}
    DELETE /comments/{comment_id}
    POST /comments/{comment_id}/vote
    POST /comments/{comment_id}/reactions
    POST /comments/{comment_id}/reports

    PUT /tasks/{task_id}/rating

具体 URL MAY 调整，但领域行为和权限语义不得改变。

## 29. 统一错误结构

所有业务错误使用稳定 code。

    {
      "error": {
        "code": "ASSIGNMENT_LIMIT_REACHED",
        "message": "当前进行中的任务已达到上限",
        "details": {
          "limit": 3
        },
        "request_id": "..."
      }
    }

至少：

- VALIDATION_ERROR
- AUTHENTICATION_REQUIRED
- PERMISSION_DENIED
- ACCOUNT_NOT_ACTIVE
- STUDENT_NOT_WHITELISTED
- PHONE_ALREADY_BOUND
- TASK_NOT_CLAIMABLE
- NO_ASSIGNMENT_AVAILABLE
- ASSIGNMENT_LIMIT_REACHED
- TASK_ACTIVE_CLAIM_EXISTS
- CLAIM_CUTOFF_REACHED
- CLAIM_NOT_SUBMITTABLE
- SUBMISSION_WINDOW_CLOSED
- FILE_TOO_LARGE
- FILE_TYPE_NOT_ALLOWED
- SUBMISSION_VALIDATION_FAILED
- ALREADY_REVIEWED
- INSUFFICIENT_POINTS
- REWARD_OUT_OF_STOCK
- REDEMPTION_LIMIT_REACHED
- RATING_NOT_ELIGIBLE

前端不得通过解析中文 message 判断业务分支。

## 30. AuditLog

字段至少：

- id
- actor_user_id
- action
- resource_type
- resource_id
- before_snapshot
- after_snapshot
- reason
- ip_address
- request_id
- created_at

至少记录：

- Admin 积分调整。
- 奖励冲销。
- Submission 通过/退回/恶意锁定失效。
- 评论管理删除。
- 匿名身份追溯。
- RewardRedemption 审批、发放。
- Task 强制关闭/状态修复。
- User suspend/ban/unban。
- StudentWhitelist 重要变更。
- 系统配置变更。
- 手工修复 Claim/Assignment 状态。

AuditLog 不允许通过普通管理后台删除。

敏感 before/after snapshot 必须脱敏，禁止把密码 hash、OTP、完整 Refresh Token 写入。

## 31. 关键数据库不变量

实现 Agent 必须优先把这些转成数据库约束、事务和测试：

1. User.username 全局唯一。
2. 规范化 phone 全局唯一。
3. Assignment 在同一 Task 的 platform + keyword 唯一。
4. 一个 Assignment 同一时刻最多一个 active Claim。
5. 同一用户同一 Task 同一时刻最多一个非终态 Claim。
6. Claim 的 ASSIGNMENT_REWARD 不能重复发。
7. TaskRating 每用户每 Task 一条。
8. CommentVote 每用户每 Comment 一条。
9. CommentReaction 每用户每 Comment 每 Emoji 一条。
10. NotificationDelivery 的 event_key + user + channel 唯一。
11. Submission version 在 Claim 内唯一。
12. Reward redemption 不能使 spendable points 为负。
13. Reward stock 不能因并发变为负。
14. 所有金额/积分 MUST 使用 integer，不使用浮点。
15. 奖励百分比计算必须明确舍入方式。

### 31.1 奖励舍入

积分为整数。

推荐：

    locked_reward_points = floor(base_reward_points * percentage)

例如：

- 101 * 80% = 80。
- 101 * 50% = 50。
- 101 * 20% = 20。

必须统一后端计算；前端只展示后端结果。

## 32. 幂等要求

以下操作必须考虑重复请求：

- 上传完成回调。
- abandon。
- submission approve。
- revision-required。
- RewardRedemption approve/reject/fulfill。
- Worker 超时释放。
- Notification send。
- 文件清理。
- 排行榜投影更新。

建议支持 Idempotency-Key 的写接口：

- reward redeem。
- upload intent / finalize。
- 高价值管理操作。

即使没有客户端 Idempotency-Key，数据库约束仍必须防止核心重复副作用。

## 33. 安全要求

### 33.1 Web

- 全站 HTTPS。
- Secure cookies。
- CSRF 防护。
- CORS 明确白名单。
- Security headers。
- 所有用户文本按纯文本处理。
- 禁止把 nickname/comment 作为 raw HTML。
- API rate limit。

### 33.2 OTP

- 短信验证码短 TTL，建议 5 分钟。
- 验证失败次数限制，建议 5 次。
- resend cooldown。
- 每手机号和每 IP 每日/每小时上限。
- OTP 不明文写日志。
- Redis 中也应存 hash 或等价不可直接读取形式。
- 验证成功后一次性消费。

### 33.3 文件

- 类型 sniffing。
- 解析器隔离。
- CPU/内存/时间限制。
- 防解压炸弹。
- 防超大行列。
- SQLite extension disabled。
- 不执行用户 SQL。
- 下载链接短时有效。
- 访问对象前验证 ownership/role。

### 33.4 后台

- Teacher/Admin 强制 2FA。
- 支持 IP/VPN 额外限制。
- 高风险操作要求二次确认。
- 匿名身份追溯、积分修改等操作写 AuditLog。
- 管理员前端不应拥有“隐藏 API”式安全假设，权限必须由后端检查。

## 34. 可观测性

至少提供：

- structured JSON logs。
- request_id / correlation_id。
- Worker job id。
- 业务错误 code。
- provider response id。
- 慢请求记录。
- /health/live。
- /health/ready。

ready 检查至少覆盖：

- PostgreSQL 可连接。
- Redis 可连接。
- 关键依赖状态可判断。

Object Storage / SMS provider 可按部署策略决定是否影响 ready，避免第三方短暂故障导致整个 API 被摘除。

## 35. 数据模型总览

Identity：

- User
- StudentWhitelist
- UserSession
- VerificationChallenge

Task：

- Task
- TaskCollaborator
- Assignment
- AssignmentClaim

Submission：

- Submission
- SubmissionValidation
- SubmissionReview
- FileObject 或等价 metadata

Points：

- PointsLedger
- PointWallet / PointProjection，可选
- PointReservation

Reward：

- RewardItem
- RewardRedemption

Ranking / Honor：

- Honor
- UserHonor
- RankingSnapshot，可选

Community：

- Comment
- CommentRevision
- CommentVote
- CommentReaction
- CommentReport
- TaskRating

Notification：

- Notification
- NotificationDelivery
- NotificationTemplate

Operations：

- AuditLog

## 36. 关键 Service 边界

实现 SHOULD 至少有语义清晰的 Service/Use Case：

Identity：

- register_student
- verify_phone
- authenticate
- rotate_refresh_session

Task：

- create_task
- publish_task
- pause_task
- import_assignments

Assignment：

- claim_random_assignment
- abandon_claim
- expire_claim

Submission：

- create_upload_intent
- finalize_upload
- validate_submission
- require_revision
- invalidate_reward_lock
- approve_submission

Points / Reward：

- grant_assignment_reward
- reverse_assignment_reward
- request_redemption
- approve_redemption
- reject_redemption
- fulfill_redemption

Community：

- create_comment
- edit_comment
- delete_comment
- vote_comment
- react_comment
- report_comment
- rate_task

Notification：

- schedule_due_notifications
- dispatch_notification

禁止在 API handler 中复制上述规则。

## 37. 测试策略

目标不是单纯追求覆盖率百分比，而是覆盖不变量、时间边界和并发竞态。

### 37.1 Unit Tests

必须覆盖：

- 学号格式。
- Unicode nickname 16 grapheme。
- 手机号规范化。
- FIXED/RELATIVE DDL。
- 奖励 100/80/50/20% 精确边界。
- 奖励整数 floor。
- revision_deadline。
- Submission Schema 字段类型。
- 评论权限。
- 评分资格。
- 荣誉条件。
- ranking_effective_at。
- 通知提醒是否应触发。

### 37.2 Integration Tests

使用真实 PostgreSQL，不能只用 SQLite 代替 PostgreSQL 测事务语义。

必须覆盖：

- SKIP LOCKED 随机领取。
- active Claim 唯一。
- 同 Task active Claim 限制。
- 每人并发 3 个限制。
- 每日放弃次数并发。
- approve 并发幂等。
- PointsLedger 唯一奖励。
- Reward reservation 双花。
- Reward stock oversell。
- Assignment import 并发唯一。
- NotificationDelivery 唯一。

### 37.3 Worker Tests

必须覆盖：

- 重复执行同一超时 job。
- 重复发送同一通知。
- Worker crash 后重试。
- 文件校验超时。
- Object Storage 临时失败。
- SMS/EMAIL provider 临时失败。
- 审核中的 Claim 不被超时释放。
- 学生刚提交与 expire worker 竞态。
- 文件清理重复运行。

### 37.4 E2E

至少：

A. 正常闭环

    白名单 -> 注册 -> 手机验证 -> 登录
    -> 浏览 Task -> 随机领取
    -> 上传合法文件 -> 机器通过
    -> Teacher approve
    -> Points 到账
    -> 排行榜变化
    -> 发起兑换 -> 冻结 -> 审核 -> fulfill

B. 逾期闭环

    领取 -> deadline 后 2h 提交
    -> 机器通过
    -> 锁 80%
    -> Teacher approve
    -> 发对应整数积分

C. Revision

    准时提交 -> 机器通过 -> 100% provisional
    -> Teacher 晚两天审核并退回
    -> revision_deadline 至少 reviewed_at +24h
    -> 学生重交
    -> approve
    -> 仍按 100%

D. 恶意空壳

    deadline 前空壳文件机器误通过
    -> Teacher invalidate reward lock
    -> 后续 deadline 后 7h 合法提交
    -> 锁 50%
    -> approve
    -> 仅获得 50%

E. 放弃

    领取 -> abandon
    -> Assignment 立即可给别人
    -> 原用户不可再次拿同 Assignment
    -> 当天第 3 次 abandon 被拒绝

F. 超时释放

    领取 -> 无有效提交
    -> grace deadline 到
    -> Claim EXPIRED
    -> Assignment AVAILABLE
    -> 另一用户可领取

G. 匿名社区

    匿名发布 -> 普通用户无法识别
    -> Teacher 可治理
    -> Admin 可追溯
    -> AuditLog 记录追溯动作

## 38. 必须实现的边界与竞态测试矩阵

### 38.1 注册

- 白名单存在 + 新手机号 -> 成功。
- 白名单不存在 -> 拒绝。
- 白名单 disabled -> 拒绝。
- 同学号两个并发注册 -> 只能一个成功。
- 两个学号同手机号并发注册 -> 只能一个成功。
- OTP 过期 -> 拒绝。
- OTP 重放 -> 拒绝。
- OTP 连续错误达到上限 -> challenge 锁定。
- nickname 16 grapheme -> 成功。
- nickname 17 grapheme -> 拒绝。
- nickname 16 个复杂 emoji -> 按 grapheme 正确判断。

### 38.2 Assignment 领取

最关键并发测试：

- Task 仅剩 10 个 AVAILABLE Assignment。
- 50 个不同用户几乎同时 POST claim。
- 最终恰好 10 个成功。
- 10 个 assignment_id 全部不同。
- 数据库没有两个 active Claim 指向同 Assignment。
- 其余请求返回 NO_ASSIGNMENT_AVAILABLE，不返回 500。

其他：

- 同一用户两个并发 claim 同一 Task -> 最多一个成功。
- 用户已有 3 个需行动 Claim -> 新 claim 失败。
- 一个 Claim 转 UNDER_REVIEW 后 -> 释放全局并行槽位。
- Task PAUSED -> 新领取失败，已有 Claim 不变化。
- FIXED 距 DDL 3h59m -> 默认 cutoff 下拒绝。
- RELATIVE 不因全局当前时间接近某日期而错误拒绝。

### 38.3 时间奖励

对每个边界做前后 1ms：

- deadline -1ms。
- deadline。
- deadline +1ms。
- +4h -1ms。
- +4h。
- +4h +1ms。
- +12h -1ms。
- +12h。
- +12h +1ms。
- grace -1ms。
- grace。
- grace +1ms。

测试服务端结果，不依赖前端倒计时。

### 38.4 Submission

- 合法 CSV。
- 缺字段 CSV。
- 重复唯一字段。
- 空文件。
- 只有 header。
- 超 min_rows 差 1 行。
- 恰好 min_rows。
- 超 max_rows。
- 超 file size。
- 假 .csv 实际 binary。
- 假 .sqlite。
- SQLite 多表但 Task 未指定 table。
- SQLite 指定不存在 table。
- XLSX 无目标 sheet。
- XLSX zip bomb 样本。
- 极长单元格。
- 解析超时。
- 同一上传完成回调重复两次。
- v1 failed，v2 valid 时 version 正确。

### 38.5 审核

- 无权限 Teacher approve -> 403。
- Task owner approve -> 成功。
- collaborator 有 review 权限 -> 成功。
- 两个 reviewer 并发 approve -> 只发一次积分。
- approve 已 COMPLETED Claim -> 幂等/ALREADY_REVIEWED。
- 审核发生在 grace 后 -> 仍可 approve 已有效提交。
- REVISION_REQUIRED 在 grace 后 -> 至少给 24h。
- revision 期间普通改错 -> 保留原 reward lock。
- invalidate reward lock -> reason 必填，AuditLog 存在。

### 38.6 积分和兑换

- 积分不足。
- 恰好足够。
- 两个并发兑换导致双花的场景。
- 最后一件库存两个并发申请 -> 最多一个占用。
- reject -> reservation 完整释放。
- approve -> reservation 转正式负流水。
- 重复 approve -> 不重复扣。
- fulfill 两次 -> 第二次幂等。
- Admin Adjustment 不改变排行榜。
- 历史 reward reversal 修正原排名周期。

### 38.7 评论

- 普通评论。
- 匿名评论。
- XSS payload 以文本展示。
- 空白评论拒绝。
- parent 跨 Task 拒绝。
- 修改别人评论拒绝。
- 删除父评论子回复仍存在。
- like 重复请求。
- like -> dislike 原子切换。
- 相同 emoji 重复点击 = toggle。
- 未完成 Task 的评分拒绝。
- 完成后评分成功。
- 同 Task 评分更新不新增第二行。

### 38.8 通知

- 领取时距 DDL 30h -> 有 24h/4h。
- 距 DDL 10h -> 仅 4h。
- 距 DDL 3h -> 不补发过去提醒。
- 在 4h 提醒前已提交 -> 4h 不发。
- validation_failed 后仍有未来 4h -> 可再次安排。
- verified email 缺失 -> email skip，不是 FAILED。
- provider timeout -> 重试。
- 重试三次 -> FAILED。
- worker 同一 job 执行两次 -> 只发送一次。

### 38.9 时区

至少构造：

- BUSINESS_TIMEZONE 午夜前后放弃次数。
- UTC 日期与业务日期不同的排行榜。
- 月末 23:59:59 与次月 00:00:00。
- DST 环境测试可使用一个有 DST 的测试时区，即使生产部署无 DST，以证明代码没有写死 +8。

## 39. 性能与容量原则

V1 不提前做微服务，但需要避免明显 O(N) 热点。

- Task 列表必须分页。
- 评论分页。
- Review queue 分页。
- AuditLog 分页。
- 排行榜 Top N 使用 Redis 投影。
- Assignment claim 查询建立 task_id + status 合适索引。
- Claim 超时扫描对 status + deadline 建索引。
- Notification due scan 对 status + scheduled_at 建索引。
- 不在请求路径全量扫描 PointsLedger 计算余额。
- 不在每次排行榜请求实时 group by 全部历史 Ledger。

性能测试具体容量由部署规模决定，但至少应对“同一分钟大量同学领取同一 Task”做压测。

## 40. 隐私原则

公开页面不得暴露：

- 学号。
- 手机号。
- 邮箱。
- 对象存储原始路径。
- 内部权限信息。

排行榜显示 nickname + honor。

匿名评论对普通用户匿名。

后台展示敏感信息遵守最小权限，并将高风险查看行为纳入审计。

日志：

- 手机号脱敏。
- 邮箱可部分脱敏。
- 不记录密码、OTP、Refresh Token。
- 文件内容默认不写应用日志。

## 41. 管理后台

Teacher 工作台：

- Task list。
- Task create/edit/publish/pause。
- Assignment import preview/confirm。
- Claim overview。
- Submission review queue。
- Submission validation report。
- Community moderation。
- Task statistics。

Admin 后台额外：

- StudentWhitelist。
- User status。
- Teacher 管理。
- RewardItem。
- Redemption 审核与 fulfill。
- Points adjustment。
- NotificationTemplate。
- Emoji whitelist。
- System config。
- AuditLog。
- Failed NotificationDelivery。
- Worker/cleanup 异常。

批量操作必须先预览后确认。

## 42. 前端关键体验

Student 首页建议展示：

- 当前 available points。
- 距离下一个 RewardItem 所需积分。
- 进行中 Claim。
- 待修改 Claim。
- 最近通知。
- 月榜排名。
- 推荐 Task。

Task Card：

- title。
- rarity。
- base reward。
- DDL 模式。
- 预计剩余时间。
- 可领取 Assignment 数量。
- rating。

领取后不暴露其他 Assignment 列表，只展示分配给本人的具体 platform/keyword。

逾期页面始终表达“当前仍可获得 X 积分”，不采用“你被扣了 X 分”的惩罚性文案。

倒计时仅用于 UX；服务端时间是最终裁决。

## 43. 激励机制

V1 奖励曲线：

- 按时：100%。
- 逾期 0–4h：80%。
- 逾期 4–12h：50%。
- 逾期 12–24h：20%。
- 24h 后无有效提交：0，释放 Assignment。

系统不因普通逾期从用户历史余额直接扣积分。

失败的代价主要是失去当前任务奖励、占用时间和放弃记录，而不是倒扣已有资产。

排行榜采用日/月/总三个时间尺度，避免总榜长期被早期头部用户锁死。

个人成长页和“我的附近”排名用于给中后排用户提供可达目标。

## 44. 业务一致性原则摘要

后续 Agent 必须牢记：

1. Task 与 Assignment 分层。
2. Assignment 与 Claim 分层。
3. Claim 与 Submission 分层。
4. Student 不能选择 Assignment。
5. 领取需要 PostgreSQL 并发锁。
6. DDL 惩罚以有效提交时间为准，不以教师审核时间为准。
7. 文件结构机器校验不等于数据质量人工验收。
8. 积分使用不可变 Ledger。
9. 可消费余额与历史贡献排名分离。
10. 匿名只影响前台展示，不破坏后台追溯。
11. Redis 是投影，不是事实源。
12. Worker 重试必须幂等。
13. 所有敏感状态变化必须服务端决定。
14. 时间计算统一使用 UTC instant + BUSINESS_TIMEZONE。
15. 数据库约束应尽量承载真正不变量，不能只相信前端和 Python if。

## 45. V1 验收标准

只有下面闭环全部满足，V1 才算实现完成：

1. 白名单学号 + 唯一手机号注册有效。
2. 学号纯数字规则和 nickname 16 grapheme 正确。
3. Teacher 能发布 Task 和批量导入唯一 Assignment。
4. 系统随机领取，不能选择 Assignment。
5. 并发领取不会重复分配。
6. 每人最多 3 个需行动 Claim，同 Task 同时最多 1 个。
7. 主动放弃每日最多 2 次并正确释放。
8. FIXED / RELATIVE DDL 均工作。
9. CSV/XLSX/SQLite 安全上传和机器校验工作。
10. 自动校验通过后进入人工验收。
11. DDL 奖励 100/80/50/20 边界正确。
12. Teacher 审核晚不会惩罚学生。
13. Revision window 正确延长。
14. 恶意空壳 reward lock 可审计地失效。
15. APPROVE 并发不重复发积分。
16. 积分兑换冻结、审核、发放正确且不能双花。
17. 日/月/总榜基于任务贡献，不受正常兑换影响。
18. 我的附近排名工作。
19. Honor 与积分资产分离。
20. 评论支持公开/匿名、回复、赞/踩、Emoji、举报。
21. 完成 Task 后可 1–5 星评分。
22. DDL -24h/-4h 多渠道通知按策略工作并防双发。
23. grace 到期无有效提交会释放 Assignment。
24. UNDER_REVIEW Claim 不会被错误释放。
25. 文件按 Task retention policy 清理。
26. Teacher/Admin 权限隔离有效。
27. 高风险动作有 AuditLog。
28. 关键错误使用稳定 code。
29. Redis 丢失后排行榜可从 PostgreSQL 重建。
30. 关键并发、边界和 Worker 重试测试全部通过。

## 46. 实现阶段建议拆分

这不是实施计划，仅用于说明领域边界。正式 Implementation Plan 必须在本规格再次经用户审核通过后另行生成。

自然拆分单元：

- Foundation：项目骨架、配置、DB、Redis、对象存储、CI。
- Identity：白名单、Student 注册、手机号、登录、2FA、RBAC。
- Task/Assignment：Task、导入、随机领取、并发控制、DDL。
- Submission：上传、文件解析、机器校验、review。
- Points/Rewards：Ledger、Wallet、Reservation、兑换。
- Ranking/Honor：Redis 投影、历史排行、成长页。
- Community：评论、匿名、Vote、Reaction、Report、Rating。
- Notification：事件、模板、SMS/Email/In-app、Worker。
- Operations：Admin、AuditLog、retention、health、observability。
- E2E/Hardening：并发、时间边界、安全样本和端到端验收。

## 47. Agent 实现纪律

后续派发的实现 Agent 必须：

- 先阅读本规格相关章节。
- 每个业务行为先写失败测试，再写实现。
- 核心状态转换必须有 Integration Test。
- 涉及时间必须使用可注入 Clock，不在业务逻辑散落 datetime.now。
- 涉及第三方 SMS/Email/Object Storage 必须通过接口适配器，测试使用 fake。
- 涉及并发正确性必须用 PostgreSQL 集成测试验证，不得仅 mock。
- 禁止为了“让测试过”放宽业务约束。
- 禁止在未更新本规格的情况下改变积分、DDL、匿名、权限等产品语义。
- 每完成一个模块，应运行该模块测试和相关跨模块回归测试。
- 对 bug 修复先添加能复现该 bug 的回归测试。

## 48. 开放但非阻塞的实现选择

以下选项不会改变产品语义，可由 Implementation Plan 最终确定：

- Celery vs RQ：推荐 Celery。
- SQLAlchemy async vs sync worker session 组合。
- SMS 供应商。
- Email 供应商。
- S3 具体厂商。
- 前端 UI component library。
- Redis 排行榜更新采用 outbox consumer 还是事务后事件。
- 是否部署独立 ClamAV。

任何选择都不得削弱本文定义的幂等、事务、权限和文件隔离要求。

## 49. 设计结论

CampusQuest V1 是一个面向校园科研/课程众包场景的任务平台，不是开放式自由接单市场。

其核心设计是：

- 管理员/教师控制任务供给。
- Assignment 预先定义，系统随机分配。
- PostgreSQL 保证领取和积分的一致性。
- 文件先机器校验，再人工验收。
- 延迟提交仍有递减奖励，而不是倒扣历史积分。
- 积分、累计贡献、荣誉分离。
- 排行榜既有总榜，也有日榜/月榜和附近排名。
- 评论允许匿名且无需预审，但后台可追溯、可治理、可审计。
- 通知与 Worker 失败不能破坏核心业务状态。
- 实现的首要目标是让并发、时间边界、重试和异常路径与正常路径一样可预测。

本文通过用户审核后，下一步应使用 Superpowers writing-plans 工作流生成详细 implementation plan，再由后续 Agent 按计划实现。