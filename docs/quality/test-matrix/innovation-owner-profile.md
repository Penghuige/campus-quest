# 负责人私有资料规约与测试

预期由2026-10-09身份基础计划推导，在实现测试前记录。

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| OP1 | 四项资料 | 四项必填、trim、Unicode上限；拒绝额外权限字段 | `tests/unit/innovation/test_owner_profile_schemas.py` | 已覆盖 |
| OP2 | 本人API | 首次空资料、真实保存读取修改；DTO白名单和禁止缓存 | `tests/integration/innovation/test_owner_profiles.py::test_private_round_trip_and_safe_audit` | 已覆盖 |
| OP3 | 身份边界 | 匿名/仅refresh Cookie拒绝；非学生与停用拒绝；无目标用户参数 | `tests/integration/innovation/test_owner_profiles.py::test_identity_cookie_and_cross_account_boundaries` | 已覆盖 |
| OP4 | 隐私 | B无A资料；审计无四项内容且读取写入有记录 | `tests/integration/innovation/test_owner_profiles.py::test_identity_cookie_and_cross_account_boundaries` + `tests/integration/innovation/test_owner_profiles.py::test_private_round_trip_and_safe_audit` | 已覆盖 |
| OP5 | 事务 | 审计失败不提交，不返回个人内容 | `tests/integration/innovation/test_owner_profiles.py::test_service_rechecks_account_and_audit_is_atomic` | 已覆盖 |
| OP6 | PG并发 | 独立连接首次及更新同版本仅一胜；独立读取提交结果 | `tests/integration/innovation/test_owner_profiles.py::test_independent_connections_create_and_update_conflict` | 已覆盖 |
| OP7 | DB约束 | 每人一行、四项非空及长度、正version | `tests/integration/innovation/test_owner_profiles.py::test_database_constraints` + `tests/integration/innovation/test_owner_profiles.py::test_independent_connections_create_and_update_conflict` | 已覆盖 |
| OP8 | 清理接线 | 仅清理指定世界资料，保留另一世界 | `tests/integration/innovation/test_world_cleanup.py::test_world_cleanup_removes_owned_drafts_and_preserves_other_world` | 已覆盖 |
| OP9 | 前端表单 | 校验/错误结果映射/旧会话读写响应丢弃 | `tests/../../frontend/src/__tests__/innovation-owner-profile.test.ts` | 已覆盖（4项单测） |
| OP10 | 浏览器 | 保存刷新修改、手机键盘、跨页冲突和显式载入 | `tests/../../frontend/e2e/innovation-owner-profile.spec.ts` | 已覆盖（真实浏览器2项通过） |
| OP11 | 账号切换 | 已显示的A四项资料在切换B后清除；与迟到响应丢弃分开验证 | `tests/../../frontend/src/__tests__/innovation-owner-profile.test.ts` + 本机 `.local-dev/verify-owner-profile.cjs` | 已覆盖（迟到响应单测；A→B真实跨标签走查通过，非标准自动电池） |
| OP12 | 稳定界面 | 固定字体Linux像素基线 | `tests/../../frontend/e2e/visual-regression.spec.ts` student-owner-profile-form；G-3 | 缺口 G-3 |

## 缺口明细

- G-3：Windows截图不可替代Linux权威PNG；本机WSL当前仅装Docker，尚无Linux Node/Chromium运行时。合并前需要生成、走查新基线。
