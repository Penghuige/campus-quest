# 测试与 CI 实践（testing-ci）

> 建立日期：2026-10-26 · 首次三源调研沉淀（材料：CQH-BATCH1 实施单三源清单，owner 决策表整体批准）。
> 用法：接到测试/CI 相关工程任务开工前自查本清单；修订旧条目保留原理由并注明变更。
> 分歧基线：外部示例与项目从严规则无方向冲突时按项目执行；外部示例永远不降低项目要求。

## 1. 测试隔离与受控数据

- **要求**：每个测试独立建立自己的数据与状态，不依赖另一个测试的执行顺序；测试触及的数据库内容必须受控（自建自清或事务回滚）。
- **理由**：顺序依赖让测试结果取决于同轮邻居，flake 与假绿同源；共享库上的测试互相污染会毁掉证据链（本仓 #76 复看清单的直接教训）。
- **来源**：Playwright Best Practices — "Make tests as isolated as possible"、"Testing with a database"（https://playwright.dev/docs/best-practices）。
- **日期**：2026-10-26。

## 2. 合并前 E2E 与报告留存

- **要求**：完整端到端套件在 pull_request 上运行；成功与失败都保存可离线阅读的报告（HTML/JUnit/JSON artifact），失败不是不留档的理由。
- **理由**：红测试也可生成报告是排障前提——报告的门禁语义只挂在证据上，不挂在通过上；留存让"当时到底跑了什么"可回溯。
- **来源**：QGIS-Website `.github/workflows/playwright-e2e.yml`（https://raw.githubusercontent.com/qgis/QGIS-Website/master/.github/workflows/playwright-e2e.yml）；本仓对应物 `make test-report`（docs/operations/test-report.md：红测试照常出报告、退出 0）。
- **日期**：2026-10-26。

## 3. 结果来源可辨识（轻量 provenance）

- **要求**：聚合展示的测试 artifact 必须能关联到产生它的工作区提交与命令——sidecar 记 `source_sha`、工作区 dirty 状态、内容哈希、命令、UTC 时间；复用旧 artifact 时按 fail-closed 判定（跨 SHA=stale、同 SHA+dirty=dirty、缺失/损坏/被改动=unknown，一律不称 fresh）。
- **理由**：一份"全绿"报告如果说不清是哪个提交跑出来的，它不是证据是装饰；就低不就高的判定防止把旧结果冒充新通过。
- **取舍**：本仓取轻量 provenance（JSON sidecar+哈希），**不引入**加密 attestation/SLSA——单仓单owner 场景下其收益不抵密钥管理成本；若未来引入外部贡献者再议升级。
- **来源**：GitHub Workflow artifacts 与 provenance 概念（https://docs.github.com/en/actions/concepts/workflows-and-actions/workflow-artifacts）；实现见 `backend/scripts/test_report.py`（provenance 节）与 docs/operations/test-report.md §2.1。
- **日期**：2026-10-26。

## 4. 项目自身从严（优先级最高）

- **要求**：以上任何外部实践不降低本仓既有要求——TDD（RED 先于实现）、集成行为在真实 PostgreSQL 验证、规约-测试矩阵（G19）、完整门禁（pre-push/verify/release-gate）、共享测试栈借用协议、证据纪律（fresh 日志才算数）。
- **理由**：外部示例是最小可行形态；本仓质量文档是产品要求。两者冲突时按本仓执行。
- **来源**：docs/quality/quality-gates.md §16、docs/quality/backend-engineering.md、`.claude/skills/campusquest-test-process/SKILL.md`、AGENTS.md「Owner acceptance」。
- **日期**：2026-10-26。
