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

## 五、teardown 审计重枚举（Task 5）

（代理执行中，完成后回填。）
