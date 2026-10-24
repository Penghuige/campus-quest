# e2e teardown 覆盖审计——写面 × 清理清单缺口矩阵

> 建立日期：2026-10-08 · 重枚举：2026-10-25（同方法重跑，新增行待 reviewer 复核） · 批准：campus reviewer（对账式方法；缺口列出不补；world clean 补齐走独立 PR 逐项）
> 审计对象：`backend/tests/e2e/browser_world.py::_clean` + `backend/tests/e2e/factories.py::clean_world`（清理引擎）× 全部 32 个 e2e spec 的**经真实表面（UI/API）落库写面**。（2026-10-08 首轮覆盖当时 14 个功能 spec；2026-10-25 重枚举补入此后新增的 14 个 spec，并首次纳入首轮未入矩阵的视觉/无障碍/smoke 家族 4 件——visual-capture、visual-regression、accessibility、cross-browser-smoke——现全量 32 件入矩阵，见下方「重枚举写面分布」。）
> 方法：枚举每个 spec 会创建哪些表行/对象 → 对照清理路径的**作用域参数**（user_ids / task_ids / reward_item_ids / honors snapshot delta / whitelist numbers / term_before）→ 判定覆盖；**实证**=有污染日志或前科记录，**推定**=写面存在且无清理路径覆盖。
> 位置说明：放 docs/quality/ 根（非 test-matrix/）——本矩阵行引用**机制代码位置**而非测试位置，混入会使 check_test_matrix 的词表校验误伤。

## 结论摘要

| 状态 | 数量 | 说明 |
|---|---|---|
| 已覆盖（引擎级联） | 22 族 | 随 user_ids/task_ids 级联删除的全部表族（2026-10-15 起新增 remembered_devices，user 域）+ S3 提交对象 + 审计双维扫描 + 设置还原 + 白名单两型 + 荣誉快照差集 + Redis 榜重建 |
| **缺口（列出不补）** | **4** | G-B（avatar MinIO 对象，**推定**）；G-C（UI 注册 6/8 前缀学号用户族，**推定**，2026-10-25 新增）；G-D（platforms 行，**推定**，2026-10-25 新增）；G-E（登录码挑战未消费行，**推定**，2026-10-25 新增） |
| 写面撤除留档 | 1 | G-A（原记 rewards-ranking 经 Admin UI 建 RewardItem；2026-10-25 复核现行 spec 集无此写面——实证记录与引擎结构性弱点仍有效） |
| 历史前科（已修复，留档） | 3 | upload_intents（已入 clean）、注册用户（drift 扫描）、第三例疑似（未复现） |
| 自过期注记 | 2 | rate-limit/OTP Redis 键（TTL 分钟级，非行级残料）；代重置密码令牌（Redis 48h TTL，2026-10-25 补录） |

## 清理引擎的作用域结构（判定基础）

`_clean` 交接四类显式 ID：**user_ids**（world 成员 + drift 扫描：注册前缀学号 + `-{run}@` 员工邮箱）、**task_ids**（world 种子 + UI 建任务按 owner 扫入）、**reward_item_ids**（**仅 world 的一个**）、**honors_before**（快照差集）。`clean_world` 现按 user/task 域级联删 **32 表**（含 sessions/TOTP/恢复码/通知/投递/荣誉授予/台账/钱包/邀请），审计行随 actor∪target 双维删（幸存者零断言兜底），S3 提交对象按 world 任务族 claims 的 intent 键删，term 设置按 before 快照还原。`_clean` 另做 Redis 榜成员摘除（仅本 student 的 zrem）与 e2e broker 队列深度还原（ltrim/DEL）——两项为既有机制，2026-10-08 版本节未记，此处补录（非新增漂移）。

**与 2026-10-08 描述的漂移（2026-10-25 复核）**：①原文记「22 表」，现行 `clean_world` 删除目标实为 32 表（audit 时点亦已达 31，原文计数失准）；窗口期内唯一新表族是 **remembered_devices**（2026-10-15 加入，user 域、先于 users 删除——V1.1 记住设备行的 FK 拦截事故修复）；②台账删除作用域现覆盖 **user_id∪operator_id 双扇入**（2026-10-09 e2e teardown 中止事故的加固，factories.py 内注释存证），预约（PointReservation）作用域加宽至 ≥ 兑换作用域（user/item/decider 并集）。其余作用域参数（user_ids drift 两型、task owner 扫入、reward_item_ids 单项、honors 差集、whitelist 两型、term 还原、审计双维）未漂移。

**结构性弱点**：凡"spec 经真实表面新建、且引擎按显式 ID 交接"的实体，若不在交接集内即成残料——RewardItem 仍是唯一此类参数（G-A 留档）。另：drift 扫描只认两类标识符形态（`3{run}` 前缀学号 / `-{run}@` 邮箱）——注册类写面选用其他学号形态即漏扫（G-C）。

## 缺口矩阵

| # | 写面来源 | 落库/落储对象 | 清理路径 | 判定 |
|---|---|---|---|---|
| G-A | rewards-ranking.spec「catalog renders, redemption consumes…」经 Admin UI 创建「端到端奖励卡<run>」 | `reward_items` 一行 | `reward_item_ids` 仅含 world 种子那一个；spec 建的**不在集合**，`delete(RewardItem).where(id.in_(items))` 不及 | 原判**实证**：2026-10-08 reviewer 验收轮，污染集成 3 例（断言奖励目录恰三样；/tmp/accept-be-integ.log:108），TRUNCATE 后 3/3 过。**2026-10-25 复核**：现行与本分支可考版本的兑换链均消费 world 种子卡（`端到端奖励卡{run[:6]}` 即种子命名契约），当前 spec 集已无经真实表面新建 RewardItem 的写面——本行转为**写面撤除留档**；引擎结构性弱点不变（reward_item_ids 仍仅 world 一项，清理中途失败的世界仍会漏该项） |
| G-B | profile-tabs.spec「avatar chain: raw-body upload → display → remove → fallback」 | e2e MinIO（:9002）`avatars/{user}/…` 对象 | 链内 remove 步会删最后一个；但 **remove→fallback→rate limit 分支后中断/失败即残留**；`users.avatar_object_key` 行随 user 级联删除，而**对象本身**不在任何清理路径（avatar 键不属 UploadIntent 扫描） | **推定**：e2e 实例为独立卷，残留仅影响该实例（跨 run 累积，无正确性影响——avatar 端点按 user_id 取当下行）；无污染日志，列为推定。（2026-10-25 复核：avatar 链仍在、引擎仍无 avatar 对象清理，判定不变） |
| G-C | **2026-10-25 新增**：spec 经真实 UI **注册的学生用户**——nickname-nudge（`6`+11 位时间戳学号）、auth-extension-2（同形 ×3/轮：通过链/拒绝链/记住设备链）、inline-register（`6`+11 位）、recent-logins 第三链（`8`+9 位） | `users` 一行（username=学号）+ 其 user_sessions；记住设备链另有 remembered_devices 子残料 | drift 扫描仅 `3{run}` 前缀学号与 `-{run}@` 邮箱两型（browser_world.py drift 段），6/8 前缀号不匹配；注册不经邮箱、无 `-{run}@` 形态；world 不导出这些注册号（无 CQ_E2E_NUDGE_NUMBER 等交接） | **推定**：写面存在且无清理路径；spec 注释自认（auth-extension-2 "registered users are teardown-invisible"、inline-register "the account is a teardown-invisible orphan"）；约 6 用户行/run 跨 run 累积（spec 以时间戳手机号规避存量行撞库，即存量残料已知存在的旁证）；残料为 PENDING/ACTIVE/REJECTED 学生行及其会话，惰性但行级 |
| G-D | **2026-10-25 新增**：platform-registry.spec 经 Admin UI「新建平台」（create→rename→disable，行留停用态、明言不删） | `platforms` 一行（code=`e2e-pf-{run[:8]}`） | `clean_world` 无 platforms 域；不属任何作用域参数；world 亦不清理种子侧（种子未建 Platform 字典行，全库唯一写面即本 spec） | **推定**：spec 注释自认（"registry rows are orphans the world teardown never sees"）；残料为停用态字典行，1 行/run 跨 run 累积，仅影响字典面渲染与 code 唯一约束空间 |
| G-E | **2026-10-25 新增**：登录码挑战**未消费行**——unified-login 错码链（1 行）与五错锁定链（1 行）、recent-logins 卡片点击 auto-send 尾链（1 行，发码后不验证即收束） | `login_email_challenges` / `login_mobile_challenges` 过期行（仅存 keyed digest，无 user FK） | 验证成功即删行、同标识符新请求替换旧行（email_login_service / mobile_login_service）；但 run 唯一邮箱/手机号永不复用 → 未消费/耗尽行无任何删除路径；user 级联不及（无 FK）；无 purge 任务（workers/ 全库核实无扫除） | **推定**：写面存在且无清理路径；残料惰性（分钟级过期摘要行，无正确性影响），约 3 行/run；与自过期注记的 Redis OTP 键不同类——此为**表行**，故入缺口而非注记 |

**补法建议（独立 PR 逐项，此处仅记）**：G-A——clean 增加按名称前缀 `端到端奖励卡{run}%` 扫描 reward_items（同 drift_users 模式）；G-B——clean 增加 `avatars/{user_id}/` 前缀对象删除（§27 守卫语义）或 e2e 实例每 run 起弃卷；G-C——world 导出各注册号入显式交接（同 CQ_E2E_REGISTER_NUMBER 模式），或 drift 扫描扩 `^[68][0-9]{9,11}$` 时间戳号位形；G-D——clean 按 code 前缀 `e2e-pf-{run}%` 扫描 platforms（同 drift 模式）；G-E——clean 按 run 邮箱/手机号前缀删未消费 challenge 行，或经裁定降级并入自过期注记。

## 已覆盖举证（22 族速览）

comments（含 revisions/votes/reactions/reports 按 comment 域）· task_ratings（task×user）· claims/assignments/tasks/collaborators（task 域，UI 建任务按 owner 扫入）· upload_intents/submissions/validations/reviews/reward_lock_history（claim 域）· redemptions/reservations/ledger/wallet（user 域 + redemption_scope，ledger 兼 operator_id 扇入）· notifications/deliveries（user 域）· user_sessions/totp_credentials/recovery_codes/remembered_devices/user_honors/staff_invitations（user 域）· honors 定义（快照差集）· audit_logs（actor∪target + 幸存者零断言）· student_whitelist（注册前缀 + 9 位邮戳正则）· system_settings（term before 还原）· S3 提交对象（intent 键 + FileNotFoundError 守卫）· Redis 榜（重建 + board_members 记账）。

## 重枚举写面分布（2026-10-25，18 件新入矩阵）

| spec | 经真实表面的写面 | 判定 |
|---|---|---|
| logout | 登录会话（world 成员 + author 学生 + 双员工） | 已覆盖（user_sessions user 域） |
| shell-refresh | 登录会话（路由拦截为客户端行为） | 已覆盖 |
| not-found | 无（匿名只读 404 面） | 无写面 |
| staff-root-redirect | 登录会话 | 已覆盖 |
| admin-invitations | StaffInvitation（admin 建）+ 审计行 + 会话 | 已覆盖（created_by user 域 + actor 域） |
| nickname-nudge | UI 注册学生（6 前缀学号）+ 会话 | **G-C**（注册行） |
| auth-extension-2 | UI 注册学生 ×3（通过/拒绝/记住设备）+ 管理员会话 + 审计行 + remembered_devices（注册用户名下） | 会话/审计已覆盖；注册族 **G-C** |
| platform-registry | Admin UI 建 platforms 行 | **G-D** |
| submission-template | 无（模板下载只读） | 无写面 |
| admin-powers | 受邀教师 ×1（`-{run}@` 邮箱）+ StaffInvitation ×2 + 代重置令牌（Redis）+ 会话 | 受邀走 drift、邀请走 created_by——已覆盖；令牌自过期 |
| inline-register | UI 注册学生（6 前缀学号）+ mobile 挑战（验证即删） | **G-C**（注册行） |
| unified-login | 受邀教师 ×3（`-{run}@`）+ 手机绑定（user 行更新）+ 会话 + 挑战行 | 受邀/会话已覆盖；错码/锁定挑战行 **G-E** |
| recent-logins | 受邀教师 ×2（`-{run}@`）+ UI 注册学生（8 前缀学号）+ 挑战行 | 受邀已覆盖；注册行 **G-C**；auto-send 未消费挑战行 **G-E** |
| switch-account | world 种子账号会话/remembered_devices + author 手机码登录 | 已覆盖（user 域） |
| visual-capture | 登录会话（渲染采集） | 已覆盖 |
| visual-regression | 登录会话（渲染比对） | 已覆盖 |
| accessibility | 登录会话（axe 采集） | 已覆盖 |
| cross-browser-smoke | 登录会话（firefox/webkit） | 已覆盖 |

（原 14 个功能 spec 的族级判定不变；本轮仅复核了其中承载缺口的 rewards-ranking（G-A）与 profile-tabs（G-B）。）

## 历史前科（已修复留档）

| 例 | 时间 | 现状 |
|---|---|---|
| upload_intents 残留（`assignment_claims←upload_intents` FK 拦 clean_world） | 2026-10-06 本会话 TRUNCATE 事故 | **已入引擎**：clean_world 现按 claim 域删 UploadIntent（factories.py:599） |
| 注册用户残留 | Phase C 记录 | **已入引擎**：drift 扫描（register_prefix） |
| 第三例疑似残料 | Phase C 记录（未定性） | 无工件可核——若再现按本矩阵方法定位；四例中唯一未闭环 |

## 自过期注记（非缺口）

rate-limit（login/otp/comments 等）与 OTP challenge 的 Redis 键按标识符留键——全部带分钟/小时级 TTL 自过期，且 db0/db15 与 e2e broker 分库，不构成行级残料。2026-10-25 补录：admin-powers 的代重置密码一次性令牌存 Redis 带 48h TTL（admin_power_service.py:238），同属自过期；登录码挑战行（login_email/mobile_challenges）验证成功即删、同标识符再请求即替换——**已消费面**自清理，**未消费面**为行级残料，已列缺口 G-E。

## 核武基线（恢复工具，非日常路径）

全库 TRUNCATE 35 表（2026-10-06 事故 playbook，本会话验证过清单与顺序）——任何新残料类型的即时恢复手段；日常清理仍以本矩阵覆盖的引擎路径为准。
