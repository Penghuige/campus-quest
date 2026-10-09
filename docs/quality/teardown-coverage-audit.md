# e2e teardown 覆盖审计——写面 × 清理清单缺口矩阵

> 建立日期：2026-10-08 · 批准：campus reviewer（对账式方法；缺口列出不补；world clean 补齐走独立 PR 逐项）
> 审计对象：`backend/tests/e2e/browser_world.py::_clean` + `backend/tests/e2e/factories.py::clean_world`（清理引擎）× 全部 14 个 e2e spec 的**经真实表面（UI/API）落库写面**。
> 方法：枚举每个 spec 会创建哪些表行/对象 → 对照清理路径的**作用域参数**（user_ids / task_ids / reward_item_ids / honors snapshot delta / whitelist numbers / term_before）→ 判定覆盖；**实证**=有污染日志或前科记录，**推定**=写面存在且无清理路径覆盖。
> 位置说明：放 docs/quality/ 根（非 test-matrix/）——本矩阵行引用**机制代码位置**而非测试位置，混入会使 check_test_matrix 的词表校验误伤。

## 结论摘要

| 状态 | 数量 | 说明 |
|---|---|---|
| 已覆盖（引擎级联） | 21 族 | 随 user_ids/task_ids 级联删除的全部表族 + S3 提交对象 + 审计双维扫描 + 设置还原 + 白名单两型 + 荣誉快照差集 + Redis 榜重建 |
| **缺口（列出不补）** | **2** | G-A（spec 建 RewardItem，**实证**=第四例残料）；G-B（avatar MinIO 对象，**推定**） |
| 历史前科（已修复，留档） | 3 | upload_intents（已入 clean）、注册用户（drift 扫描）、第三例疑似（未复现） |
| 自过期注记 | 1 | rate-limit/OTP Redis 键（TTL 分钟级，非行级残料） |

## 清理引擎的作用域结构（判定基础）

`_clean` 交接四类显式 ID：**user_ids**（world 成员 + drift 扫描：注册前缀学号 + `-{run}@` 员工邮箱 + UI 建任务的 staff owners）、**task_ids**（world 种子 + UI 建任务按 owner 扫入）、**reward_item_ids**（**仅 world 的一个**）、**honors_before**（快照差集）。`clean_world` 内 22 表全按 user/task 域级联（含 sessions/TOTP/恢复码/通知/投递/荣誉授予/台账/钱包/邀请），审计行随 actor∪target 双维删（幸存者零断言兜底），S3 提交对象按 world 任务族 claims 的 intent 键删，term 设置按 before 快照还原。

**结构性弱点**：凡"spec 经真实表面新建、且引擎按显式 ID 交接"的实体，若不在交接集内即成残料——RewardItem 是唯一此类参数。

## 缺口矩阵

| # | 写面来源 | 落库/落储对象 | 清理路径 | 判定 |
|---|---|---|---|---|
| G-A | rewards-ranking.spec「catalog renders, redemption consumes…」经 Admin UI 创建「端到端奖励卡<run>」 | `reward_items` 一行 | `reward_item_ids` 仅含 world 种子那一个；spec 建的**不在集合**，`delete(RewardItem).where(id.in_(items))` 不及 | **实证**：2026-10-08 reviewer 验收轮，污染集成 3 例（断言奖励目录恰三样；/tmp/accept-be-integ.log:108），TRUNCATE 后 3/3 过 |
| G-B | profile-tabs.spec「avatar chain: raw-body upload → display → remove → fallback」 | e2e MinIO（:9002）`avatars/{user}/…` 对象 | 链内 remove 步会删最后一个；但 **remove→fallback→rate limit 分支后中断/失败即残留**；`users.avatar_object_key` 行随 user 级联删除，而**对象本身**不在任何清理路径（avatar 键不属 UploadIntent 扫描） | **推定**：e2e 实例为独立卷，残留仅影响该实例（跨 run 累积，无正确性影响——avatar 端点按 user_id 取当下行）；无污染日志，列为推定 |

**补法建议（独立 PR 逐项，此处仅记）**：G-A——clean 增加按名称前缀 `端到端奖励卡{run}%` 扫描 reward_items（同 drift_users 模式）；G-B——clean 增加 `avatars/{user_id}/` 前缀对象删除（§27 守卫语义）或 e2e 实例每 run 起弃卷。

## 已覆盖举证（21 族速览）

comments（含 revisions/votes/reactions/reports 按 comment 域）· task_ratings（task×user）· claims/assignments/tasks/collaborators（task 域，UI 建任务按 owner 扫入）· upload_intents/submissions/validations/reviews/reward_lock_history（claim 域）· redemptions/reservations/ledger/wallet（user 域 + redemption_scope）· notifications/deliveries（user 域）· user_sessions/totp_credentials/recovery_codes/user_honors/staff_invitations（user 域）· honors 定义（快照差集）· audit_logs（actor∪target + 幸存者零断言）· student_whitelist（注册前缀 + 9 位邮戳正则）· system_settings（term before 还原）· S3 提交对象（intent 键 + FileNotFoundError 守卫）· Redis 榜（重建 + board_members 记账）。

## 历史前科（已修复留档）

| 例 | 时间 | 现状 |
|---|---|---|
| upload_intents 残留（`assignment_claims←upload_intents` FK 拦 clean_world） | 2026-10-06 本会话 TRUNCATE 事故 | **已入引擎**：clean_world 现按 claim 域删 UploadIntent（factories.py:530） |
| 注册用户残留 | Phase C 记录 | **已入引擎**：drift 扫描（register_prefix） |
| 第三例疑似残料 | Phase C 记录（未定性） | 无工件可核——若再现按本矩阵方法定位；四例中唯一未闭环 |

## 自过期注记（非缺口）

rate-limit（login/otp/comments 等）与 OTP challenge 的 Redis 键按标识符留键——全部带分钟/小时级 TTL 自过期，且 db0/db15 与 e2e broker 分库，不构成行级残料。

## 核武基线（恢复工具，非日常路径）

全库 TRUNCATE 35 表（2026-10-06 事故 playbook，本会话验证过清单与顺序）——任何新残料类型的即时恢复手段；日常清理仍以本矩阵覆盖的引擎路径为准。
