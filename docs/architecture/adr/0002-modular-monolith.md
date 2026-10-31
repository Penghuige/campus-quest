# ADR 0002: 模块化单体，配机器可检的架构契约

日期: 2026-10-31 · 状态: 已接受
关联: docs/quality/backend-engineering.md §1/§2 · docs/architecture/as-built.md ·
docs/architecture/interfaces.md · backend/pyproject.toml `[tool.importlinter]`

## 决策

后端是一个**模块化单体**：按业务域切模块（identity / tasks / submissions /
points / rankings / community / notifications / audit / files / system /
innovation），每个域内固定分层
`router → application/domain service → repository/query/port → infrastructure`，
事务边界在服务层。跨模块只读他域事实时走**目录端口**（interfaces.md 的
Identity directory 规则），不 import 他域 ORM 模型。

分层的成立不靠自觉，靠**三件机器可检的契约**：

1. **import-linter 分层契约**（`[tool.importlinter]`，`make arch-lint`）——
   每个域一条 layers 契约（router 在 service 之上，service 不 import router），
   外加三条全域禁令：模块不得 import worker 运行时（两条已登记的组合缝隙
   除外）、core 是底层不得 import 模块、identity 的 ORM 模型不得被域外引用。
2. **豁免即债务台账**——已知的漂移不静默放行也不硬堵：像 pip-audit 豁免
   一样显式登记 `ignore_imports`（notifications 直读 identity.models 的
   5 处漂移在案），整改 = 删豁免行。
3. **as-built 真相图**（`make arch-map` 生成 `as-built.md`，含跨模块依赖
   有向图与 import 计数；`make arch-city` 出代码城市可视化）——门禁宽恕
   已登记的漂移，地图如实报告树的真实形状。

## 理由

- 单团队、单部署单元、域间调用全是进程内函数调用——微服务在这个规模
  只购买网络跳数与运维税，不购买任何隔离（域边界用契约守，比用
  网络守便宜且可测试）。
- 事务正确性（跨 points/submissions/audit 的多表状态迁移）在单库单连接
  上是一个 `COMMIT`；拆服务的第一天就要引入分布式事务或最终一致，
  复杂度买不到对价收益。
- 模块化的真实风险是**腐化**（分层被一次"临时 import"击穿），所以治理
  投在机器检查而不是评审自觉上：门禁红 = 合并阻断。

## 被否决的替代

- **微服务/按域拆库**：见上；且本仓库的并发不变量（ADR 0003）依赖
  同库行锁与约束，拆库即失效。
- **仅靠评审守分层**：历史证明漂移会积累；豁免台账 + 自动化门禁把
  "知道欠债"和"还债"变成两个显式动作。

## 后果

- 新增 `*_service.py`/`*_router.py` 需同步登记该域的 layers 契约
  （门禁报错会点名缺哪层）——这是有意的摩擦：改架构必须是显式动作。
- 域内重构自由；跨域读事实先问端口有没有，没有就先立端口。
- 未来若真要拆：as-built 图就是拆分边界的现成输入，契约与端口已经是
  模块间的准 API——单体不是拆不掉，是现在不值得拆。
