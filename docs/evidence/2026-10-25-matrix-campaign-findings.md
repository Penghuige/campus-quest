# 测试覆盖战役发现清单（2026-10-25）

> 战役分支：`test-coverage-campaign`（worktree，基线 origin/main @ 68eba1b）
> 性质：**只记录，不修复**（矩阵纪律：缺口列出不补；修复走独立任务）。
> 关联计划：`docs/superpowers/plans/2026-10-25-test-coverage-campaign.md`

## 一、checker 名称级校验首跑捕获（Task 1）

升级 `check_test_matrix.py` 到名称级引用校验后，首跑 13 份存量矩阵捕获 **7 处陈旧引用**：

| 矩阵 | 行 | 判定 | 处置 |
|---|---|---|---|
| claims-lifecycle | C23 | 测试搬家（→ `tests/integration/submissions/test_validation_worker.py`） | 修正引用路径 |
| login-iteration | LI13-LI16 | **端点族整体退役**（legacy staff 码流，PR-3；语义由 `unified-login.md` U3/U5/U8/U10/U11/U20 承接） | 四行改判流程行（已被取代） |
| notifications | N3 | 测试搬家（→ `tests/unit/notifications/test_deadline_scheduler.py`） | 修正引用路径 |
| submissions-validation | S12 | 测试搬家（→ `tests/integration/submissions/test_reward_lock.py`） | 修正引用路径 |

结构性观察：**裸路径引用（不带 `::名字`）只能验证文件存在**——`test_staff_mobile_code_login_api.py` 从整族端点测试缩到 2 件时，引用它的裸路径行全部静默通过。名称级引用是今后建行的默认格式。

## 二、七个域对账结果（Task 2，矩阵 13→20 份）

| 域矩阵 | 覆盖 | 缺口（轻） | 不匹配 |
|---|---|---|---|
| deadline.md（§9） | 7 | 0 | 0 |
| task-ratings.md（§20 补充面） | 4 | 1（G-5：TaskRating 字段集无显式冻结断言） | 0 |
| rewards-redemption.md（§16） | 11 | 3 注记（G-1 建议字段无落库断言；G-2 UNDER_REVIEW 中间态无 producer 无测试；G-3 FULFILLED 永久消耗无行为断言） | 0 |
| honors.md（§18） | 7 | 1 行 1 注记（G-1 **周期榜荣誉 producer 未实现**——周期结算→授荣调度链路缺失，docstring 载明留 Plan 07/08；G-2 display_honor 无 HTTP/UI 面） | 0 |
| growth-page.md（§19） | 12 | 0 | **1（GP2）**：本月排名"变化"半边三层一致缺位（GrowthResponse 无字段、冻结断言主动排除、前端无映射）——实现缺口或 spec 修订，上报裁定 |
| audit-log.md（§30） | 11 | 4（G-1 Task 级状态修复命令不存在；G-2 StudentWhitelist 审计对象已随开放注册退役=spec 过时行；G-3 EMAIL_LOGIN_SUCCEEDED/TOTP_REQUIRED 审计行**零测试**（写行在 email_login_service 四处，旧断言件随 PR-3 退役）；G-4 MOBILE_LOGIN_SUCCEEDED 审计行零测试） | 0 |
| file-retention.md（§13+§27） | 13 | 3（G-1 retention 枚举闭集无插库拒绝测试；G-2 默认 DAYS_180 无显式断言；G-3 清理后审计行存续无显式断言） | 0 |

值得 owner 注意的**产品级**发现（非测试缺口）：
1. **GP2 不匹配**（growth-page）：成长页"本月排名及变化"的"变化"半边 API/前端/测试三层一致缺位，且集成冻结断言把该字段排除在 GrowthResponse 外——要么补实现、要么修 spec §19，需 review 裁定（本次不动）。
2. **周期榜荣誉链路未实现**（honors G-1）：`evaluate_honors` 只有服务层直调测试，今日/本月卷王与月度 Top 3 的自动触发不存在（留 Plan 07/08）。
3. **display_honor 设置面不可达**（honors G-2）：服务层语义已钉，但无端点无 UI。
4. **UNDER_REVIEW 中间态无 producer**（rewards G-2）：兑换决策器接受该态但无人置入；V1 从 REQUESTED 直达决策与 spec 状态图的字面关系待 review 裁定。
5. **登录成功审计行零断言**（audit-log G-3/G-4）：EMAIL_LOGIN_SUCCEEDED/TOTP_REQUIRED/MOBILE_LOGIN_SUCCEEDED 写行存在但测试零覆盖（旧断言件随 PR-3 端点退役被删，新统一族未补对应审计断言）。
6. **spec §30 StudentWhitelist 行过时**（audit-log G-2）：白名单管理面已随 V1.1 开放注册退役，spec 待修订。

## 三、G-3 闭合（Task 3）

claims-lifecycle C3：Claim 状态转移集中化 pin 由 `tests/unit/tasks/test_claim_lifecycle_property.py` 补齐（spec 抄录 oracle 八边 + 确定性/终态吸收/全可达性质 + 生产位点 parity：转移仅存于五个归属 service、终态转移单一归属）。缺口 1→0。

## 四、变异战役（Task 6，unit 范围 12 变异体）

工具：一次性脚本（逐体替换→跑 `tests/unit/tasks` + `tests/unit/points`→还原；基线 281 passed；目标文件 `claim_service.py` + `ledger_service.py`）。**3 杀 / 9 存活**：

| 变异体 | 位点 | 变异 | 判定 |
|---|---|---|---|
| M1 | claim_service:491 | cutoff `<` → `<=` | **KILLED**（单测钉死） |
| M2 | claim_service:491 | `now + cutoff` → `now` | **KILLED** |
| M3 | claim_service:500 | 配额 `>=` → `>` | **KILLED**（off-by-one 被钉） |
| M4 | claim_service:905 | 到期守卫 `<` → `<=` | 存活（unit） |
| M5 | claim_service:905 | 到期守卫禁用 | 存活（unit） |
| M6 | ledger_service:234 | 零额守卫 `==0` → `<=0` | 存活（unit） |
| M7 | ledger_service:440 | `>0` → `>=0` | 存活（unit） |
| M8 | ledger_service:473 | `available +=` → `-=` | 存活（unit） |
| M9 | ledger_service:478 | `earned +=` → `-=` | 存活（unit） |
| M10 | ledger_service:559 | 正额守卫 `<=0` → `<0` | 存活（unit） |
| M11 | ledger_service:450 | 流水符号翻转 | 存活（unit） |
| M12 | ledger_service:440 | touches_earned 恒 False | 存活（unit） |

解读（初判，非结论）：**领取资格/配额/cutoff 的边界已被单测钉死；钱包算术、流水符号、到期守卫在单层无断言**——与架构一致（账本走真 PG 集成，到期走 worker/e2e）。9 个幸存者在借栈窗口内做集成级交叉验证（`tests/integration/points`、`tests/integration/tasks`、`tests/workers`）后分类：caught-by-integration（防线只是分层不同）或 genuine assertion gap（补钉任务另立）。

**交叉验证终判（基线 376 passed；每体单独注入后全量复跑）**：
- **7/9 被集成层击杀**（M4、M5 到期守卫；M6 零额守卫收紧；M8/M9 钱包符号；M11 流水符号；M12 earned 触发）——防线分层成立，无行动项。
- **M7（`amount > 0` → `>= 0`）= 等价变异体**：上游 :234 已拒 `amount == 0`，该点两种写法语义相同（M6 被秒杀即证明 :234 先行）。非缺口。
- **M10（任务奖励 `<= 0` → `< 0`）= 唯一真·轻缺口**：零分 ASSIGNMENT_REWARD 边界在单测与集成两层都无人断言（正数不变量只在负数侧被钉）。列出不补，一行测试可闭合。

## 五、teardown 审计重枚举（Task 5）

审计对象 14 → **32 个 spec**（新增 18 件全部分级：12 已覆盖 / 2 无写面 / 5 涉缺口）。新增缺口 3 项（均推定，列出不补）：

- **G-C**：经真实 UI 注册的学生（时间戳学号 6/8 前缀）不在 drift 扫描集——nickname-nudge / auth-extension-2 / inline-register / recent-logins 四 spec 每轮约 6 个 users 行 + sessions/remembered_devices 子残料跨 run 累积（spec 注释自认 "teardown-invisible orphan"）。
- **G-D**：platform-registry.spec 经 Admin UI 建的 platforms 字典行无清理域（create→rename→disable 留停用态）。
- **G-E**：`login_email_challenges`/`login_mobile_challenges` 未消费行（验证即删但 run 唯一标识永不复用且无 purge 任务），约 3 行/轮。

原有缺口：G-A 写面已撤除（现行 spec 集无经真实表面新建 RewardItem 的链路；结构性弱点不变，2026-10-08 实证留档）；G-B avatar 对象不变。引擎描述漂移已修正（clean_world 实为 **32 表**非 22；台账 user∪operator 双扇入；补记 Redis 榜 zrem 与 broker 队列还原；新表族 remembered_devices）。历史前科表未动；新增行待 campus reviewer 复核（文档头已注明）。

**G-C 实锤升级（同日双杀门禁两条腿，建议高优先排期清理修复）**：本战役的 fresh release-gate 中，G-C 残料（前一轮电池跑留下的 UI 注册学生按用户名排序挤占目录首页）先后击穿 `admin-powers.spec`（reset-token 链找不到目录行）与 `visual-regression` 的 `admin-users` 基线（表高 1422→1820px 内容差）。两次均以栈重置恢复，未改基线。

## 六、门禁全程揪出并已在本分支修复的三处存量债

1. **backend-e2e 两处过期契约**（开放注册批 8da07d5 落地于 CI 停摆期，e2e 未跟）：注册信封 `{user, tokens}` + PENDING_APPROVAL 需 admin approve 解锁；统一登录 identifier 表单与嵌套 tokens；whitelist 管理面 404 语义（436aa0d）。
2. **Makefile 两条腿缺 CI 同款环境**（talent-batch1 已有同型修复未合）：`playwright-e2e` 与 `visual-regression` 缺 `CQ_E2E_LOGIN_RATELIMIT=1` + `CQ_E2E_API_URL`（87dfb14 + 后续 commit）。
3. **共享测试库被 talent-batch1 分支迁移戳污染**（`0035_talent_file_upload_contract`），按 release-checklist 手册程序重置恢复。

## 七、十二缺口闭合批（2026-10-26，owner 授权"未完成项现在做"）

分支 `test-gap-closures`（基 main bbbb941），三代理并行，**12/12 全部处置**：

| 缺口 | 处置 | 落点 |
|---|---|---|
| points-ledger G-1 | **勘误闭合**：冻结测试 2026-10-10 已在 main（766cd7a），战役盘点定位遗漏；矩阵翻转+勘误注记 | points-ledger.md E16 |
| points-ledger G-2 | 新钉：五表八数量列 Integer/BigInteger 闭集 + 六模型无浮点扫掠 | test_points_column_types.py |
| submissions G-4 | 现状在（schema 255 上限 + sanitize，函数级测试已有）→ 补恰值/超限/空四边界 | test_upload_policy.py 追加 |
| task-ratings G-5 | 新钉：字段闭集（五字段+代理键）+ 类型冻结；改评 updated_at 前移（独立会话双真提交形态） | test_task_rating_model.py + test_ratings.py |
| rewards G-1 | 新钉：建议字段全链往返（create→列→学生目录缺席/staff 列表在场） | test_points_api.py |
| rewards G-3 | 新钉：FULFILLED 永久消耗（stock=1 全链后第二人 409 + CONSUMED/available 断言） | test_redemption_concurrency.py |
| audit-log G-3 | 新钉：email 登录全审计行（SUCCEEDED×channel / TOTP_REQUIRED / remembered_device 变体），4 写行点全覆盖 | test_login_audit_rows.py |
| audit-log G-4 | 新钉：学生/员工 mobile 登录审计行（G11 隐私 sweep 内嵌全部 4 件） | 同上 |
| file-retention G-1 | 新钉：retention CHECK 闭集四参插库拒 | test_task_constraints.py |
| file-retention G-2 | 新钉：默认 DAYS_180（expunge 重读形态，迁移丢默认可抓） | 同上 |
| file-retention G-3 | 新钉：清理删对象后审计行逐字段存续（真 PG 分相+finally 清理） | test_cleanup_audit_survival.py |
| M10（变异战役） | 新钉：任务奖励 0/-1 拒于首条数据库语句前（哨兵会话证明） | test_task_reward_amount_guard.py |

- 措辞映射注记（非矛盾）：spec「mobile +device 快照」的设备变体仅存在于 email ADMIN 记住设备腿；mobile 学生腿快照恒 `{channel: mobile_code}`——已记 audit-log.md 备注，待 owner 备览。
- 验证：checker 28 份 OK；单测 1450 全绿（标准 TEST_STACK_ENV 形态）；三连绿；分支零生产代码改动。
- 集成腿（8 件）借栈窗口实跑，结果回填于下。

**借栈窗口回填（2026-10-26）**：8 件集成钉实跑 **13 passed**（含文件内全部用例；test_cleanup_audit_survival 首跑暴露夹具未走 presign 流程的假件前置，按 test_file_cleanup 先例修复后过，修复随分支提交）。十二缺口闭合批全数落地。
