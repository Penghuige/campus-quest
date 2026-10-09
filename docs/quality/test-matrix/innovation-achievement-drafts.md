# 成果私有草稿规约与测试

预期来自独立需求PR-08/11/12和2026-10-09计划，写于测试与实现之前。

| # | 规则引用 | 预期 | 测试位置 | 状态 |
|---|---|---|---|---|
| AD1 | 私有准备 | 仅名称必填，四字段trim/长度；作品链接限制http(s)、禁账号密码；额外字段拒绝；修改需显式四字段防误清空 | `tests/unit/innovation/test_achievement_schemas.py` | 已覆盖 |
| AD2 | 保存闭环 | 自己项目下创建多份、分页/详情/修改，DTO不含账号与授权字段，no-store | `tests/integration/innovation/test_achievement_drafts.py::test_round_trip_idempotence_and_safe_audit` | 已覆盖 |
| AD3 | 多重隔离 | 正常学生+本人项目+对应成果；跨账号、错项目、不存在404；非学生/停用/匿名/cookie拒绝 | `tests/integration/innovation/test_achievement_drafts.py::test_parent_child_and_account_boundaries` | 已覆盖 |
| AD4 | 新鲜权限 | 旧Actor状态/角色变化，服务重新核验；运营不自动可读他人成果 | `tests/integration/innovation/test_achievement_drafts.py::test_audit_failure_and_fresh_service_state` + `tests/integration/innovation/test_achievement_drafts.py::test_parent_child_and_account_boundaries` | 已覆盖 |
| AD5 | 创建幂等 | 独立连接同key只建一份；原payload重试返回当前内容，不覆盖更新；不同payload409 | `tests/integration/innovation/test_achievement_drafts.py::test_independent_creation_and_version_races` + `tests/integration/innovation/test_achievement_drafts.py::test_round_trip_idempotence_and_safe_audit` | 已覆盖 |
| AD6 | 修改并发 | 独立连接同version仅一胜；等待账号锁时停用则拒绝 | `tests/integration/innovation/test_achievement_drafts.py::test_independent_creation_and_version_races` + `tests/integration/innovation/test_achievement_drafts.py::test_waiting_create_observes_committed_suspension` | 已覆盖 |
| AD7 | 审计 | 读写持久审计，不复制文字/链接；审计失败回滚 | `tests/integration/innovation/test_achievement_drafts.py::test_round_trip_idempotence_and_safe_audit` + `tests/integration/innovation/test_achievement_drafts.py::test_audit_failure_and_fresh_service_state` + `tests/integration/innovation/test_achievement_drafts.py::test_read_and_update_cannot_succeed_without_audit` | 已覆盖 |
| AD8 | 数据库 | FK、正version、长度和唯一约束；测试世界只清理自己的成果；迁移down/up | `tests/integration/innovation/test_achievement_drafts.py::test_database_constraints` + `tests/integration/innovation/test_world_cleanup.py`; 独立迁移库0028→0027→0028成功 | 已覆盖 |
| AD9 | 前端 | 输入校验、请求键稳定、会话迟到响应丢弃、冲突保留并显式载入 | `tests/../../frontend/src/__tests__/innovation-achievements.test.ts` + `tests/../../frontend/e2e/innovation-achievements.spec.ts` | 已覆盖 |
| AD10 | 浏览器 | 通过本人项目进入，手机键盘保存/刷新/编辑/纯文本私有预览，多页冲突，丢失创建回包与换账号 | `tests/../../frontend/e2e/innovation-achievements.spec.ts` | 已覆盖（真实浏览器3项） |
| AD11 | 稳定界面 | 固定字体Linux像素基线及选择器契约 | `tests/../../frontend/e2e/visual-regression.spec.ts` student-achievement-draft-form；选择器已登记 | 缺口 G-2 |

## 缺口明细

- G-2：合并前须补Linux权威像素PNG，不以Windows截图替代。

## 2026-10-09 本地验证

前端全量612项单测通过、0跳过；后端创新模块集成+创新/core单测148项通过（真实PG）；双创四组浏览器10项通过（包含新增成果3项）、0跳过。前端type/lint/CSS通过；后端ruff和mypy通过。OpenAPI与类型已同步。Windows桌面冲突与手机私有预览截图已实际走查，文字按文本渲染、移动端无横向溢出。

最终生成类型后再次typecheck/lint/CSS及Next生产构建通过，新增动态成果路由已生成。开发库仅升级到0028并重启自有前后端，未回退/清库。日志位于项目外 `.local-dev/logs/achievement-{frontend-unit,backend,e2e,build}.log`，不作为源码上传。

初次账号锁并发断言把停用错误误写成PERMISSION_DENIED，按现有身份契约修为ACCOUNT_NOT_ACTIVE；产品预期“停用后拒绝”未改。随后补失败测试修复PATCH漏字段静默清空隐患，要求显式传四字段。上述148为该修复后的新鲜结果。独立审查、Linux像素、完整发布电池/覆盖率仍为合并前要求，此记录不是发布或合并批准。
