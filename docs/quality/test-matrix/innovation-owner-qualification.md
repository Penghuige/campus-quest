# 负责人资格开通规约对账

预期依据用户方案A及 `docs/superpowers/plans/2026-10-09-innovation-owner-qualification.md`，先于测试实现落笔。

Q11为后续Actor核验补测的对账行，依据既有G10/G11/G12鉴权契约登记实际执行证据。

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| Q1 | 方案A：显式申请 | 保存四项不授权，申请后PENDING，ADMIN确认才APPROVED，全局角色及运营授权不变 | `tests/integration/innovation/test_owner_qualification.py::test_real_http_application_and_admin_activation` | 已覆盖（本轮后端模块回归通过） |
| Q2 | 计划：快照版本 | 资料更新不替换已申请快照，显式更新申请后旧ADMIN版本409，不撤销已开通资格 | `tests/integration/innovation/test_owner_qualification.py::test_snapshot_update_and_stale_admin_decision` | 已覆盖（HTTP与真实PG） |
| Q3 | G10/G11/G12 | 无Bearer/仅Cookie401；非学生/停用拒绝；ADMIN需TOTP和管理网络；运营学生不可读申请PII，队列和本人状态不含四项 | `tests/integration/innovation/test_owner_qualification.py::test_authority_and_private_field_boundaries` + `tests/integration/innovation/test_owner_qualification.py::test_extra_fields_and_management_network` + `tests/integration/innovation/test_owner_qualification.py::test_refresh_cookie_alone_cannot_read_or_apply_qualification` + `tests/integration/innovation/test_owner_qualification.py::test_operations_student_cannot_manage_owner_qualifications` | 已覆盖（G-4已闭合：针对性权限补验通过） |
| Q4 | G12/G15 | 独立PG连接并发申请/批准仅一次；审计失败不泄漏、不提交，快照PII不进审计 | `tests/integration/innovation/test_owner_qualification.py::test_independent_pg_connections_transition_once` + `tests/integration/innovation/test_owner_qualification.py::test_failed_audit_prevents_apply_reveal_and_approval` + `tests/integration/innovation/test_owner_qualification.py::test_real_http_application_and_admin_activation` | 已覆盖（独立连接申请/批准竞争、审计失败回滚及安全审计） |
| Q5 | G2/G19 | 实际浏览器申请、开通、刷新；手机键盘、冲突重读、换账号清除；契约/选择器/视觉工件随实现 | `tests/../../frontend/e2e/innovation-owner-qualification.spec.ts` + `tests/../../frontend/e2e/innovation-owner-profile.spec.ts` + `tests/../../frontend/e2e/accessibility.spec.ts` + `tests/../../frontend/e2e/visual-regression.spec.ts` | 已覆盖（本功能G-1/G-2/G-3已闭合；全站视觉与发布限制仍见G-5） |
| Q6 | 输入与数据库不变量 | 申请仅接收资格版本和已保存资料版本，拒绝夹带资料或权限字段；PG强制版本、状态与批准字段组合 | `tests/integration/innovation/test_owner_qualification.py::test_extra_fields_and_management_network` + `tests/integration/innovation/test_owner_qualification.py::test_qualification_database_constraints` | 已覆盖（HTTP额外字段拒绝及数据库约束） |
| Q7 | G19：世界清理 | 仅清理指定世界资格记录，保留另一世界；批准人外键关联在世界范围外时拒绝扩大清理范围 | `tests/integration/innovation/test_world_cleanup.py::test_world_cleanup_removes_owned_drafts_and_preserves_other_world` + `tests/integration/innovation/test_world_cleanup.py::test_world_cleanup_handles_approved_qualification_scope` | 已覆盖（本轮后端模块回归包含清理测试） |
| Q8 | 前端申请与请求隔离 | 未保存资料或脏输入不可申请；PENDING仅允许新已保存版本更新，APPROVED不可覆盖；409/未知结果需重读；跨账号迟到响应丢弃 | `tests/../../frontend/src/__tests__/innovation-owner-qualification.test.ts` | 已覆盖（8项资格纯函数/API测试；UI功能及本功能无障碍另见已闭合G-1/G-2） |
| Q9 | G19：契约工件 | OpenAPI快照、生成类型、资格页面选择器契约随API和页面实现同步 | `frontend/openapi.snapshot.json` + `frontend/src/lib/api/schema.d.ts` + `docs/quality/e2e-selector-contract.md` | 流程行（生成契约与选择器已同步；执行门禁不由工件存在性替代） |
| Q10 | 完整验收边界 | 相关模块通过不能替代完整后端、覆盖率棘轮、发布电池或独立审查 | — | 缺口：G-5（全量、覆盖率与工作树复核证据已取得；完整发布、全站视觉及最终HEAD CI/合并审查仍未闭合） |
| Q11 | G10/G11/G12：新鲜Actor核验 | 合法Bearer解析Actor后，资格服务加锁重读当前学生/管理员角色、状态及管理员TOTP确认；申请人权限变化或批准等待锁期间停用均拒绝，资格及审计不产生被拒操作的副作用 | `tests/integration/innovation/test_qualification_actor_recheck.py::test_student_service_rechecks_after_actor_resolution` + `tests/integration/innovation/test_qualification_actor_recheck.py::test_admin_service_rechecks_after_actor_resolution` + `tests/integration/innovation/test_qualification_actor_recheck.py::test_admin_service_rechecks_changed_applicant` + `tests/integration/innovation/test_qualification_actor_recheck.py::test_waiting_approval_observes_applicant_suspension` | 已覆盖（独立Linux真实PG后续补测文件10项通过，含参数化Actor核验及锁等待用例） |

## 本轮已取得的执行证据

- 后端 `tests/unit/innovation tests/unit/core tests/integration/innovation`：164 passed、2 warnings、退出0。包含资格文件13项实际执行用例（含参数化并发和约束），以及资格接入的测试世界清理；完整日志为工作区 `.local-dev/logs/qualification-backend-module.log`。这不是全部后端测试通过。
- 主任务本轮报告后端mypy检查177个文件通过、ruff通过。此处只记录该范围，不据此声明完整发布门禁通过。
- 权限补验后的资格文件15 passed、9.92s、退出0；完整日志为工作区 `.local-dev/logs/qualification-guards-final.log`。包含仅refresh Cookie不能代替Bearer，以及已有运营授权的学生仍不能管理资格申请或读取申请PII的明确测试；据此闭合G-4。
- 后端全量在Linux独立测试库的两批pytest执行：unit/worker 1266 passed、2 warnings、351.81s，`UNIT_WORKERS_EXIT=0`；integration 1134 passed、7 deselected、29 warnings、846.06s，`INTEGRATION_EXIT=0`。两批共2400项通过，完整日志为工作区 `.local-dev/logs/qualification-linux-final-backend.log`；7项deselected按日志保留，不改写为全选零跳过。该次全量采集的innovation覆盖率96.9低于原地板97.8，`COVERAGE_RATCHET_EXIT=1`，失败记录保留。
- 随后新增`tests/integration/innovation/test_qualification_actor_recheck.py`在同Linux独立库真实执行10项（含参数化）全部通过、9.57s、`ACTOR_RECHECK_EXIT=0`，完整日志为工作区 `.local-dev/logs/qualification-actor-guards.log`。除Q11新鲜Actor与锁等待核验外，包含未申请资料不得揭示/批准以及当前资格版本重复提交同资料版本409；与先前2400项全量分开报告，不称为一次2410项全量批次。
- 上述10项采用同一个coverage collector的`--cov-append`累积采集；innovation最终98.1高于原地板97.8，全部18个模块的地板保持不变，`COVERAGE_RATCHET_EXIT=0`。完整覆盖率报告为工作区 `.local-dev/logs/qualification-linux-final-coverage-with-actor-tests.json`，格式/Ruff/mypy新鲜结果亦在`qualification-actor-guards.log`：383 files already formatted、All checks passed、177 source files无问题；该补验整批`EXIT_CODE=0`。
- 主任务报告独立Agent工作树复核无阻塞问题；复核对象是HEAD `284345a`之上的未提交树，不是提交级APPROVE，也不代替最终HEAD的CI或合并审查。
- 前端全量纯测624 passed、0 failed、0 skipped，完整日志为工作区 `.local-dev/logs/qualification-frontend-unit.log`。资格新增测试先出现5项预期行为失败，再通过8项；负责人资料/运营/资格集中回归17项通过。
- 前端typecheck、lint、check:css及正式build退出0；生成契约绑定后的typecheck和build完整日志分别为工作区 `.local-dev/logs/qualification-frontend-typecheck-contract.log`、`.local-dev/logs/qualification-frontend-build.log`。正式构建包含 `/admin/owner-qualifications`。
- 管理队列分页修复后的最终前端源码树复跑：全量纯测624 passed、0 failed、0 skipped；typecheck、lint、check:css、正式build和coverage:ratchet均退出0。覆盖率为lines 95.41、branches 91.45、functions 90.26，高于棘轮95.21/91.22/90.19。完整日志为工作区 `.local-dev/logs/qualification-frontend-final-unit.log`、`qualification-frontend-final-typecheck.log`、`qualification-frontend-final-lint.log`、`qualification-frontend-final-css.log`、`qualification-frontend-final-build.log`、`qualification-frontend-final-coverage-ratchet.log`，各日志均保存完整命令输出和退出码。
- 本轮浏览器功能：工作区 `.local-dev/logs/qualification-browser-module.log`中的7项功能测试全部通过（运营2、负责人资料2、资格3）；`.local-dev/logs/qualification-browser-drafts.log`中的6项私有项目/成果草稿功能测试全部通过，合计13项功能测试全部执行，据此闭合G-1。同次module日志中的axe为15 passed、1 failed（student-tasks），属于保留的历史失败，不能据此宣称最终axe通过。
- 最后仅调整SHOTS准备条件后的typecheck、lint、check:css复跑均退出0；完整日志为工作区 `.local-dev/logs/qualification-frontend-typecheck-final-surfaces.log`、`qualification-frontend-lint-final-surfaces.log`、`qualification-frontend-css-final-surfaces.log`。准备条件等待真实内容及有限动画完成，不修改生产样式、axe基线或业务行为。
- 最终axe在SHOTS准备条件补齐后的完整日志为工作区 `.local-dev/logs/qualification-axe-content-ready.log`：16项全部通过原双向棘轮，未增加或删除豁免。结合390px移动端真实流程的键盘、Dialog焦点恢复与无横向溢出走查，本功能G-2已闭合；历史中间态/加载态失败记录继续保留。
- 固定字体Linux视觉：仅新增`admin-owner-qualifications-linux.png`并更新`student-owner-profile-form-linux.png`两张资格相关基线，其他既有PNG未更新；最终`admin-owner-qualifications`、`student-owner-profile-form`、`admin-innovation-operations`、`admin-users`四个相关页面均通过。全16页比较为15 passed、1 student-tasks failed，继承的15142像素差异保留为G-5全站限制，不用本功能完成覆盖它。完整日志为工作区 `.local-dev/logs/qualification-visual-compare.log`、`qualification-visual-update.log`、`qualification-visual-final-matrix.log`，逐张走查见`docs/quality/innovation-owner-qualification-visual-walkthrough.md`。
- 上述数字属于已经完成的这轮执行记录。后续修复或新增测试后必须更新为最终树的新鲜结果；本功能浏览器、axe及相关视觉证据不从历史结果借用，完整发布电池、全站视觉限制、最终HEAD CI和合并审查仍见G-5。

## 缺口明细

- G-1（已闭合）：本轮真实浏览器资格申请→ADMIN显式查看快照→确认开通→学生重读/刷新、PENDING更新快照与管理员旧页409、网络已提交但丢回包后重读、跨标签换账号清除，以及刷新后空队列分页导航均通过；运营授权、负责人资料与私有项目/成果草稿功能回归亦通过。两个完整日志`qualification-browser-module.log`和`qualification-browser-drafts.log`分别取得7项和6项功能通过，合计13项全部执行。
- G-2（本功能已闭合）：390px移动端真实流程已执行键盘Enter、取消/Escape焦点恢复、开通后重读焦点和无页面横向溢出；最终`qualification-axe-content-ready.log`取得16项axe全部通过原双向棘轮。历史module批次的15 passed、1 student-tasks failed仍作为中间态失败保留，不替换最新证据，也不增减豁免。
- G-3（本功能已闭合）：只新增ADMIN资格页和更新负责人资料页两张Linux PNG；两张最终基线均经人工走查，运营管理及用户管理页保持既有PNG字节。最终四个资格/管理导航相关页面全部通过原比较阈值；全16页比较的student-tasks继承差异15142像素仍失败，归G-5全站限制，不宣称完整像素门禁通过。
- G-4（已闭合）：`test_refresh_cookie_alone_cannot_read_or_apply_qualification`明确覆盖仅refresh Cookie不能读取或提交资格申请；`test_operations_student_cannot_manage_owner_qualifications`明确覆盖已有运营授权的学生不能读取资格管理队列/资料详情或执行批准。主任务在补验后的资格文件取得15 passed、9.92s、退出0，完整日志为工作区 `.local-dev/logs/qualification-guards-final.log`。
- G-5：已取得后端2400项全量两批通过、另10项真实PG补验通过、同collector追加采集后全部18模块原地板通过，以及`284345a`之上未提交树独立Agent复核无阻塞问题的证据；最初innovation 96.9低于97.8的棘轮失败记录保留。完整发布电池、最终HEAD CI和合并审查仍未闭合；工作树复核不等于提交级APPROVE。全站视觉为15/16通过，student-tasks的15142像素继承差异尚未闭合。已通过的本功能门禁不构成完整发布门禁通过或合并/生产上线批准。
