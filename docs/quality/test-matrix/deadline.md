# Deadline 规约-测试对账矩阵

> 建立日期：2026-10-25 · 域：Deadline——FIXED/RELATIVE 领取时限、claim cutoff、UTC 时间边界（test-coverage-campaign 批次，agent A）
> 方法论（owner 纪律）：**预期列先于测试检索从 spec 原文推导落笔**，然后逐条定位测试；找不到测试=缺口（列出不补）；测试行为与预期不符=不匹配（上报不擅改——可能是 bug 也可能是 spec 缺口，由 review 裁定）。
> 覆盖章节：§9
> 规约来源：`docs/superpowers/specs/2026-09-19-campusquest-design.md` §9(+9.1-9.3)；§6.2 MUST-快照列、§31.1 floor 语义交叉（后者见 points 矩阵 E20）。
> 边界让渡（不重复建行）：§9.1 默认 cutoff 拒领与恰值边界 → `claims-lifecycle.md` C29；§9.2 RELATIVE 口径（claimed_at+duration）→ C30；§9.3 五档分段、四恰值与 ±1ms 十二边界 → `submissions-validation.md` S1（其规约来源明列 §9.3）；100%/80% 档锁档集成面 → C20。
> 接线：`backend/scripts/check_test_matrix.py`（手动跑；名称级校验）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 7 | D1-D7 |
| 缺口（轻，列出不补） | 0 | — |
| 不匹配 | 0 | — |
| 流程行 | 1 | D8（让渡地图 + G13 元规则） |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| D1 | §9.1 首句 + 领取式 | FIXED：领取时 deadline_at = Task.fixed_deadline_at，与 claimed_at 无关——所有 Claim 共享同一 instant | `tests/unit/tasks/test_deadlines.py::test_fixed_mode_uses_task_instant_grace_plus_24h`（deadline 取 FIXED_AT）+ `tests/unit/tasks/test_deadlines.py::test_fixed_mode_ignores_claimed_at`（早晚两个领取时点结果相同）+ `tests/integration/tasks/test_claim_concurrency.py::test_claim_snapshot_survives_later_task_edits`（集成面：claim.deadline_at==fixed_deadline_at 落库断言） | 已覆盖 |
| D2 | §9.1/§9.2 领取式第二行 | grace_deadline_at = deadline_at + 24h，恰 24h（两种模式同式）；server_default 未应用的未落库行走列默认 1440m 兜底，不产生 None | `tests/unit/tasks/test_deadlines.py::test_grace_is_exactly_24h_in_both_modes`（FIXED/RELATIVE 参数化恰值差）+ `tests/unit/tasks/test_deadlines.py::test_relative_grace_falls_back_to_column_default`（grace_period_minutes=None → 仍 +24h） | 已覆盖 |
| D3 | §9.1 "cutoff 可配置" | cutoff 可配置（实现为 Task 级 claim_cutoff_minutes）：默认 240m；自定义 30m 时余 29m 拒、恰 30m 可；显式零 cutoff 不拦 | `tests/unit/tasks/test_claim_eligibility.py::test_cutoff_is_configurable`（cutoff_minutes × remaining 参数矩阵，含 zero-cutoff-at-deadline-allowed）+ `tests/integration/tasks/test_task_api.py::test_teacher_lists_and_reads_own_and_collaborated_tasks`（创建/详情面 claim_cutoff_minutes==240 默认值可读） | 已覆盖（默认值下的领取侧拒领行为让渡 claims C29，不重复建行） |
| D4 | §9.1 末条 | 发布时离 deadline 已不足 cutoff：typed 错误明确拒绝、保持 DRAFT、零提交——禁止领取优先于挂网，杜绝"负剩余时间"任务上线；恰 cutoff 可发布；resume 再入 PUBLISHED 同门复验（暂停期间窗口变陈旧不得复活） | `tests/unit/tasks/test_task_lifecycle.py::test_publish_fixed_within_claim_cutoff_blocked`（60/239m 双点：TASK_NOT_CLAIMABLE + DRAFT + commits==0）+ `tests/unit/tasks/test_task_lifecycle.py::test_publish_fixed_exactly_at_cutoff_allowed`（240m 恰值可发布且 claimable）+ `tests/unit/tasks/test_task_lifecycle.py::test_entry_into_published_revalidates_stale_fixed_deadline`（publish/resume 共享目标检查的回归钉；提醒窗口行为另见 notifications 矩阵 N4，跨域引） | 已覆盖 |
| D5 | §9.2 末句 | 之后 Teacher 修改 duration 不影响已存在 Claim（FIXED 侧修改 fixed_deadline_at 同理）：已算得的 deadline/grace 快照不动，且任务行本身确已变更（非侥幸相等） | `tests/unit/tasks/test_deadlines.py::test_relative_snapshot_survives_task_edit`（duration→9999、grace→7 后 result 不变，重算确实不同）+ `tests/unit/tasks/test_deadlines.py::test_fixed_snapshot_survives_task_edit`（fixed_deadline_at→+30d 后不变）+ `tests/integration/tasks/test_claim_concurrency.py::test_claim_snapshot_survives_later_task_edits`（集成面：改 fixed_deadline_at 后 claim 的 deadline/grace 两列冻结；与 claims C20 共享证据、本行规则切片为 §9 时限快照） | 已覆盖 |
| D6 | §9.3 首句 | 所有比较使用 UTC instant：naive datetime 一律拒绝（submitted_at/deadline_at/grace_deadline_at 三口 + claimed_at + fixed_deadline_at 口），aware 间的比较即即时比较，杜绝墙上时钟直比 | `tests/unit/tasks/test_deadlines.py::test_reward_fraction_rejects_naive_datetimes`（三口参数化，按槽名报错）+ `tests/unit/tasks/test_deadlines.py::test_compute_rejects_naive_claimed_at` + `tests/unit/tasks/test_deadlines.py::test_compute_rejects_naive_fixed_deadline` | 已覆盖（aware-only API 为执法口径，注记见末节） |
| D7 | §9.3 MUST 末句 | 避免前后端各自使用不同的 < / <=：档位计算后端唯一，前端 server-value-only——渲染行不得出现百分比/档位数字，数值为快照 verbatim（0.8x 客户端梯会把 200 算成 160，需钉死不可发生） | 前端 never-compute pin：`frontend/src/__tests__/reward-view.test.ts::the number is the snapshot itself, never a ladder multiple` + `frontend/src/__tests__/reward-view.test.ts::pre-deadline: the snapshot renders verbatim for any value`（两例均过 assertNoClientMath：无 %、无"档位 N"）；后端十二边界梯 `tests/unit/tasks/test_deadlines.py::test_reward_fraction_boundary_ladder`（分段行为本体让渡 submissions S1） | 已覆盖 |
| D8 | 让渡地图 + G13（quality-gates §16） | §9 的分段恰值/默认拒领/RELATIVE 口径已由邻域矩阵建行，本矩阵不重复；上述语义变更必须先改 spec | ——（流程行：C29/C30/S1 各行即其机制） | 流程行 |

## 缺口明细（列出不补）

无。§9 各可观察行为均定位到测试（含让渡至邻域矩阵的行）。

## 不匹配明细

无。本域未发现测试行为与 spec 推导预期冲突的条目。

## 备注（边界与覆盖形态注记）

- **让渡明细**（同一测试文件承载多域行为，行内交叉引用、不重复建行）：§9.1 "距 deadline 少于 4 小时停止新领取"+恰 cutoff 边界 → `claims-lifecycle.md` C29（`test_fixed_task_past_cutoff_refuses_claims` / `test_fixed_task_exactly_at_cutoff_still_claims`）；§9.2 deadline_at=claimed_at+duration 口径 → C30（`test_relative_mode_uses_claimed_at_plus_duration`）；§9.3 五档分段、四个恰值与 ±1ms 十二边界 → `submissions-validation.md` S1（`test_reward_fraction_boundary_ladder`，该矩阵规约来源明列 §9.3）；floor 取整（§31.1，101×80%=80 家族）→ `points-ledger.md` E20。
- D6 的"UTC instant"以 aware-only API 执法：拒绝 naive 输入即消除墙上时钟比较；aware datetime 之间的比较在语义上就是 instant 比较（跨时区偏移等价）。spec 未要求显式的跨偏移换算用例，此口径为已验证的实现执法面。
- D3/D4 的 cutoff 语义为"严格小于才拦"（余量恰等于 cutoff 时仍可领取/发布），与 §9.1 "少于 4 小时"的措辞一致，两侧（领取面 C29 / 发布面 D4）边界口径互证。
