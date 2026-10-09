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
| OP9 | 前端表单 | 校验/错误结果映射/旧会话读写响应丢弃；5xx要求读取确认并保留追踪号 | `tests/../../frontend/src/__tests__/innovation-owner-profile.test.ts` | 已覆盖（6项单测） |
| OP10 | 浏览器 | 保存刷新修改、手机键盘、跨页冲突和显式载入 | `tests/../../frontend/e2e/innovation-owner-profile.spec.ts` | 已覆盖（真实浏览器2项通过） |
| OP11 | 账号切换 | 已显示的A四项资料在切换B后清除；与迟到响应丢弃分开验证 | `tests/../../frontend/src/__tests__/innovation-owner-profile.test.ts` + 本机 `.local-dev/verify-owner-profile.cjs` | 已覆盖（迟到响应单测；A→B真实跨标签走查通过，非标准自动电池） |
| OP12 | 稳定界面 | 固定字体Linux像素基线 | `tests/../../frontend/e2e/visual-regression.spec.ts` student-owner-profile-form | 已覆盖（Linux生成、走查及重复比较通过） |
| OP13 | 四字段编辑边界 | 编辑状态不继承版本或资格元数据；修改输入不改写已保存资料 | `tests/../../frontend/src/__tests__/innovation-owner-profile.test.ts` editing an owner profile includes only self-reported fields, never qualification or version | 已覆盖（单测通过） |

## 2026-10-09 覆盖率补验预期

依据身份基础计划的四字段资料与保存结果不明处理约定：编辑状态仅包含四项资料，不继承版本或资格等元数据；停用/无权限不给保存成功提示；服务端 5xx 可能在提交后发生，须读取确认并保留请求追踪号，明确的 4xx 拒绝不要求按未知提交结果处理。先记录预期再补单测；资料保存仍不授予负责人资格。

## 缺口明细

- G-3 已闭合：Linux 初始 PNG 已生成、走查及重复比较通过；详见 `docs/quality/innovation-linux-visual-walkthrough.md`，不等同完整像素门禁通过。
