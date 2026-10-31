# ADR 0006: 证据文化——棘轮、对账矩阵、双审与"新鲜门禁才算验收"

日期: 2026-10-31 · 状态: 已接受
关联: Makefile（coverage-ratchet / ratchet-direction / pre-push /
release-gate）· docs/quality/quality-gates.md §16 ·
docs/quality/test-matrix/ · frontend/e2e/noskip-exemptions.mjs ·
docs/loadtest/capacity-report-2026-10.md · AGENTS.md「Owner acceptance」

## 决策

质量主张必须**可复核**，所以门禁的设计原则是"只进不退 + 留痕"：

1. **覆盖率棘轮**（coverage-ratchet）：模块级地板只升不降
   （`--update` 走登记路径并把理由写进同一 PR），另有方向门禁
   （ratchet-direction）堵住"悄悄调低地板"的洞——地板是债务台账
   的反面镜像。
2. **规约-测试对账矩阵**（docs/quality/test-matrix/）：每个业务域维护
   "spec 条款 ↔ 测试"对照，预测在先、定位在后；改动域必须同步矩阵行
   （G19 工件清单的一部分）。
3. **e2e 不许静默跳过**：no-skip 断言 + 豁免登记制
   （noskip-exemptions.mjs，带门禁与理由）——"绿"必须意味着"跑了"。
4. **双审流**：实现方视角 + 独立 reviewer 视角，判词必须点名确切 HEAD；
   合并授权归 owner，且 owner 验收只认**新鲜跑出的**门禁输出——
   复用旧日志不算证据。
5. **CI 停摆期的替代事实源**：CI 配额暂停期间，本地 `make verify` →
   `make release-gate` 的新鲜输出是唯一验收证据（预检清单与已知
   分歧记录在案）；per-HEAD 的工件（门禁日志、再生的契约工件、
   QA 表关闭记录）随 PR 交付，不做"事后补"。

## 理由

- "测试挺全的"是不可复核的主张；"地板 95.4，降了就红"是可复核的事实。
  棘轮把质量从形容词变成单调量。
- 对账矩阵把 spec（人类意图）与测试（机器验证）钉在一起，覆盖率
  数字因此有语义：知道哪些条款有测试，才知道没测的是什么。
- 双审与 owner 复核的分工是"每顶帽子只戴一次"：实现者不给自己判绿，
  reviewer 不行使合并权，owner 不轻信转述数字。

## 被否决的替代

- **一次性覆盖率数字作为门槛**：只防"没测"，不防"变差"。
- **只在 CI 上看质量**：CI 也会停（配额、事故），验收链条不能有
  单点。

## 后果

- 新增/移动测试面要走的清单在 AGENTS.md 常驻（G19）；"裸 pytest 绿"
  不等于"棘轮绿"是登记在案的教训。
- 容量证据沿用同一哲学（ADR 0001）：数字必须由留存的 run 工件支撑，
  报告里的每个数都能对账到 summary.json/checks.json。
