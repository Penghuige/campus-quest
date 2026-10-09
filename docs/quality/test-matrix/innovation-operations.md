# 双创运营授权测试矩阵

| # | 规则引用 | 预期 | 测试位置 | 状态 |
|---|---|---|---|---|
| OG1 | 输入 | 原因去空白必填且有上限；严格非负整数版本；拒绝额外字段 | `tests/unit/innovation/test_operations_schemas.py` | 已覆盖 |
| OG2 | 授权生命周期 | 未授权 0 → 授予 1 → 撤回 2 → 重授 3；同状态或旧版本 409 | `tests/integration/innovation/test_operations_grants.py::test_http_lifecycle_and_scope` | 已覆盖 |
| OG3 | 管理员门禁 | 匿名、学生、教师、停用管理员、未确认 TOTP、网络策略不符拒绝 | `tests/integration/innovation/test_operations_grants.py::test_http_guards` + `tests/integration/innovation/test_operations_grants.py::test_actual_store_network_policy_and_validation` | 已覆盖 |
| OG4 | 目标限制/新鲜权限 | 仅 ACTIVE STUDENT 可被授予；被停用/改角色仍可撤回；旧 Actor 角色/状态/TOTP 拒绝 | `tests/integration/innovation/test_operations_grants.py::test_target_rules_fresh_admin_and_revocation` | 已覆盖 |
| OG5 | 并发 | 独立连接同时首次授予仅一笔成功；旧撤回不能移除重授；等待锁期间账号停用后拒绝 | `tests/integration/innovation/test_operations_grants.py::test_independent_grants_and_stale_revoke` + `tests/integration/innovation/test_operations_grants.py::test_waiting_grant_observes_committed_suspension` | 已覆盖 |
| OG6 | 事务 | 审计异常时授权不落库；审计仅状态/版本 | `tests/integration/innovation/test_operations_grants.py::test_audit_failure_rolls_back_grant` + `tests/integration/innovation/test_operations_grants.py::test_http_lifecycle_and_scope` | 已覆盖 |
| OG7 | 权限边界 | 运营仍 STUDENT，不能进入管理员/积分管理；无他人负责人资料端点 | `tests/integration/innovation/test_operations_grants.py::test_http_lifecycle_and_scope` | 已覆盖 |
| OG8 | 前端请求 | scoped API、预期版本、迟到的跨账号响应丢弃；结果不明/409须重读 | `tests/../../frontend/src/__tests__/innovation-operations.test.ts` | 已覆盖 |
| OG9 | 真实闭环 | 管理员授予 → 学生显示 → 确认撤回 → 学生更新；旧页冲突/丢失回包 | `tests/../../frontend/e2e/innovation-operations.spec.ts` | 已覆盖（真实浏览器2项） |
| OG10 | 数据库 | 0026→0027→0026→0027；外键/约束；测试世界保留其他世界授权 | `tests/integration/innovation/test_operations_grants.py::test_grant_database_constraints` + `tests/integration/innovation/test_world_cleanup.py` | 已覆盖（迁移手动实跑） |
| OG11 | 稳定界面 | 固定字体Linux像素基线 | `tests/../../frontend/e2e/visual-regression.spec.ts` admin-innovation-operations | 缺口 G-2 |
| OG12 | 会话切换 | 已显示授权页面在换账号后清除旧状态 | `tests/../../frontend/e2e/innovation-operations.spec.ts` genuine admin grant；同源另一标签 A→B→A，无刷新更新身份 | 已覆盖 |

实施前建立以上预期。当前真实 PG 和核心/双创单元回归 112 项通过，前端完整单元 608 项通过。执行证据只代表本轮本地验证，未执行项目不标为通过。

## 缺口明细

- G-1 已闭合：运营2项 + 草稿3项 + 负责人资料2项，共7项真实浏览器通过，零跳过。手机390×844无横向溢出；桌面/手机截图已人工走查。
- G-2：Windows 截图不可替代 Linux 权威 PNG；合并前须生成并走查新页面及管理导航变化影响的既有 admin-users 基线。
- G-3 已闭合：补充 A→B→A 真实跨标签切换后，两项运营浏览器测试再次通过（42.5 秒，零跳过），已挂载个人页无需刷新清除旧授权。
