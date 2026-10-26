# 个人成长页 规约-测试对账矩阵

> 建立日期：2026-10-25 · 域：个人成长页（§19 展示指标族 + 按时完成判定三规则；测试覆盖活动 test coverage campaign 任务 C）
> 覆盖章节：§19
> 方法论：`campusquest-test-process` skill 规约对账节；**预期列先于测试检索从 spec 原文落笔**；缺口列出不补；不匹配上报不擅改。
> 规约来源：spec §19（按时完成定义引用 §9.3 锁档阶梯 / §11.3 恶意空壳重锁 / §11.4 revision 保档语义）。
> 域界：积分与榜单口径（REDEMPTION 不计榜、ADMIN_ADJUSTMENT 榜中性、冲销修期、榜单 DTO 隐私）在 `points-ledger.md` E5/E6/E12/E14/E16——成长页读同一 PG 聚合与同一 Redis 月榜，口径行不重复建行；荣誉授予机制（§18）未入任何矩阵，本表只对账 §19 的"已获得荣誉"展示行。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 12 | GP1、GP3-GP13 |
| 缺口（列出不补） | 0 | — |
| 不匹配 | 1 | GP2（"本月排名及变化"的"变化"半边三处缺位） |
| 流程行 | 0 | — |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| GP1 | §19 第 1 项 | 成长页展示本月获得积分（业务时区月聚合；兑换等非 ranking 流水不计入） | `tests/integration/rankings/test_growth_api.py::test_growth_me_uses_final_valid_lock_for_on_time_ratio`（month_points==300，spend −50 对一切榜面数字不可见）+ `frontend/src/__tests__/growth-view.test.ts::every §19 figure maps 1:1 onto display text`（monthPoints 120 → 展示文本） | 已覆盖 |
| GP2 | §19 第 2 项 | 成长页展示本月排名**及变化** | 排名半边已覆盖：`tests/integration/rankings/test_growth_api.py::test_growth_me_uses_final_valid_lock_for_on_time_ratio`（month_rank==2，读与月榜同一 Redis 板）+ `frontend/src/__tests__/growth-view.test.ts::null ranks read as 未上榜 / 暂无 (the server's no-score verdict)`；**"变化"半边三处缺位**：`GrowthResponse`（backend/app/modules/rankings/router.py:142-154）无任何变化字段、`_GROWTH_FIELDS` 冻结断言（test_growth_api.py:89-99 与 :419 的 `set(body) == _GROWTH_FIELDS`）钉死契约不含变化、前端 `growthView` 无变化映射（growth-view.test.ts 全族无此断言面） | 不匹配 |
| GP3 | §19 第 3 项 | 成长页展示总 earned points（全期 ranking 聚合） | `tests/integration/rankings/test_growth_api.py::test_growth_me_uses_final_valid_lock_for_on_time_ratio`（total_earned_points==700=300+400，spend 不计）+ `frontend/src/__tests__/growth-view.test.ts::every §19 figure maps 1:1 onto display text`（totalEarnedPoints） | 已覆盖 |
| GP4 | §19 第 4 项 | 成长页展示累计完成任务数（COMPLETED Claim 计数） | `tests/integration/rankings/test_growth_api.py::test_growth_me_uses_final_valid_lock_for_on_time_ratio`（completed_count==3）+ `frontend/src/__tests__/growth-view.test.ts::every §19 figure maps 1:1 onto display text`（completedCount） | 已覆盖 |
| GP5 | §19 第 5 项 | 成长页展示按时完成率（按时/完成；无完成时不造假 0%） | `tests/integration/rankings/test_growth_api.py::test_growth_me_uses_final_valid_lock_for_on_time_ratio`（on_time_ratio≈1/3）+ `frontend/src/__tests__/growth-view.test.ts::no completions yet: no ratio figure instead of a fake 0%` + `frontend/src/__tests__/growth-view.test.ts::ratio percent keeps one decimal when the float needs it` | 已覆盖 |
| GP6 | §19 第 6 项 | 成长页展示当前连续按时数（newest-first 连续 tier-100 段长，遇非按时即止） | `tests/integration/rankings/test_growth_api.py::test_growth_me_uses_final_valid_lock_for_on_time_ratio`（current_streak==1：步进止于重锁 late claim）+ `frontend/src/__tests__/growth-view.test.ts::every §19 figure maps 1:1 onto display text`（streakText「连续按时 4 次」） | 已覆盖 |
| GP7 | §19 第 7 项 | 成长页展示最佳历史月排名（历史月独立于本月；无历史则空） | `tests/integration/rankings/test_growth_api.py::test_growth_me_uses_final_valid_lock_for_on_time_ratio`（best_month_rank==1，与本月 rank 2 区分；PG 重算非 Redis 抓取）+ `frontend/src/__tests__/growth-view.test.ts::null ranks read as 未上榜 / 暂无 (the server's no-score verdict)`（bestRank null 透传） | 已覆盖 |
| GP8 | §19 第 8 项 | 成长页展示已获得荣誉（荣誉服务唯一词汇，无第二套荣誉名单） | `tests/integration/rankings/test_growth_api.py::test_growth_me_uses_final_valid_lock_for_on_time_ratio`（honors=={首次完成任务, 累计获得 500 积分}；行字段集恰 {honor_id,name,honor_type,period,granted_at}）+ `frontend/src/__tests__/growth-view.test.ts::a honor row carries name/type/period + a business-tz date`（业务时区日期走 tz 数据库）+ `frontend/src/__tests__/growth-view.test.ts::the frozen auto types + commemorative map to zh-CN wording`（§18 六类措辞冻结、未知类型降级不漏枚举） | 已覆盖 |
| GP9 | §19 按时定义第 1 条 | 按时判定=首个最终有效 reward lock 的 submitted_at<=deadline_at（实现代理：该锁落 §9.3 阶梯 on-time 臂 tier 100；late 臂 80 不计） | `tests/integration/rankings/test_growth_api.py::test_growth_me_uses_final_valid_lock_for_on_time_ratio`（late tier-80 不计、ontime tier-100 计；判定查询与荣誉服务共享同一 §19 词汇——backend/app/modules/rankings/honor_service.py completion_counts/current_on_time_streak） | 已覆盖 |
| GP10 | §19 按时定义第 2 条 | 之后 revision 不改变"是否按时"判定（§11.4：奖励档位仍保持首个未被判无效的有效提交） | `tests/integration/submissions/test_reward_lock.py::test_later_valid_revision_does_not_lower_existing_lock`（后续有效 revision 不降既有锁档）+ `tests/integration/submissions/test_review_flow.py::test_late_review_sets_revision_deadline_and_preserves_reward`（退回保档）——档位保持 ⇒ 成长侧 tier==100 判定随之不变（机制联动面；成长侧判定本体见 GP9） | 已覆盖（联动） |
| GP11 | §19 按时定义第 3 条 | 被 INVALIDATE_REWARD_LOCK 的空壳提交不能让任务被视为按时（deadline 前的空壳不算，重锁钳 ≤20%） | `tests/integration/rankings/test_growth_api.py::test_growth_me_uses_final_valid_lock_for_on_time_ratio`（shell 场景：deadline 前 provisional 100 档被 INVALIDATE、有效 LATE 提交重锁 tier 20 → on_time_count==1；测试 docstring 钉「the shell's pre-deadline submission must NOT make its claim on-time」） | 已覆盖 |
| GP12 | §19（学生个人面语义；RBAC 总纲 G10 见 identity 矩阵） | 成长页仅学生本人可用：非学生 403 PERMISSION_DENIED；未认证 401 | `tests/integration/rankings/test_growth_api.py::test_growth_me_rejects_non_student_roles` + `tests/integration/rankings/test_growth_api.py::test_growth_me_requires_authentication` | 已覆盖 |
| GP13 | §19（成长页前端落位） | 个人主页"我的档案"tab 渲染"我的成长"区域（摘要+荣誉合成）；"个人信息"tab 不渲染；垃圾 tab 值降级回 landing 不出错误页 | `frontend/e2e/profile-tabs.spec.ts::bare /profile lands on 我的档案 with both tabs announced`（成长岛在档、info 面不在）+ `frontend/e2e/profile-tabs.spec.ts::garbage tab values degrade to the landing tab, never an error page` + `frontend/src/__tests__/growth-view.test.ts::growthView composes summary + honors` | 已覆盖 |

## 缺口明细（列出不补）

无。

## 不匹配明细

- **GP2**（§19 第 2 项「本月排名及变化」）：排名本身三面齐（API 值、前端"第 N 名/未上榜"映射、与月榜同板），但 spec 要求的「变化」元素在三层一致缺位——后端 `GrowthResponse` 无变化字段、集成测试的 `_GROWTH_FIELDS` 冻结断言主动排除了任何变化字段、前端 `growthSummaryView` 无变化映射。即现行测试钉死的契约**不含** spec「至少展示」清单中的这一元素。按纪律上报不擅改：可能是实现缺口（需补字段+测试），也可能是 spec 需求已过时待修订（review 裁定；若裁定为实现缺口，补测试按缺口纪律另开任务，本表不补）。

## 备注（实现取舍的裁决记录，非缺口）

- 按时判定的实现代理是 `reward_tier_locked == 100`（§9.3 阶梯 on-time 臂唯一给 100；§11.3 重锁钳制 ≤20），等价于 spec 的「首个最终有效 reward lock 的 submitted_at<=deadline_at」——`honor_service` 与 `growth_service` 共享同一查询（一判定词汇），荣誉授予与成长页不可能对同一 Claim 给出相反的按时结论。
- 「本月排名」读 Redis 月榜（与 `/rankings/monthly` 同板同数），「最佳历史月排名」从 PG 账本重算（Redis 是可重建投影，§17.3）——两口径的差异是设计取舍，见 `growth_service` 模块 docstring 的并列说明。
