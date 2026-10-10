# R2 更新复审验证回执

2026-10-10 开始，2026-10-11 完成本地门禁及交付收口。范围基线 `3431b20fb7b8c6ffd7c87276516e0598a3929b16`；后端最终源 `6e95815`，产品及测试最终源 `b3d35d0`。后续提交只记录质量文档，不改产品、测试或基线。远端最终提交核验另记，不能用历史 R1 的测试或 CI 代替。

## 当前源码验证

| 验证 | 本轮结果 | 完整日志（工作区 .local-dev/logs） |
| --- | --- | --- |
| 后端格式、lint、mypy | 全部出口 0；mypy 192 源文件 | r2-isolated-full-backend.log |
| 单元／worker | 1307 passed，280.67s | 同上 |
| 真实 PostgreSQL 集成 | 1235 passed，7 非 integration 标记排除，877.22s；组合 smoke 均启用 | 同上 |
| greenlet + thread coverage | 棘轮通过；双创 98.4%，地板 97.8%；从新文件采集，不合并历史数据 | 同上、r2-isolated-full-greenlet.json |
| 后端端到端 | 42 passed，149.09s | 同上 |
| 后端依赖审计 | 无已知漏洞，出口 0 | 同上 |
| 迁移模型对账 | Alembic check 无新增升级操作，出口 0 | r2-alembic-isolated-check.log |
| 矩阵及 API 工件 | 14 矩阵通过，OpenAPI 与实际生成一致 | 全量后端日志；最终回执再次检查矩阵 |
| Windows 前端六项门禁 | typecheck/lint/check:css/unit/coverage/build 全部出口 0；641 passed、无跳过；95.49 / 91.60 / 90.45，地板 95.21 / 91.22 / 90.19 未降低 | r2-frontend-{typecheck,lint,check-css,test-unit,coverage-ratchet,build}.log |
| Important 修复的真实浏览器 | 旧文案 4 断言失败（1 test），修复后 1 passed，50.6s；提交和退回仍不可见，提示不再声称公开 | r2-down-copy-red.log、r2-down-copy-green.log |
| 原有截图 | 16 passed，1.4min，无重拍开关 | r2-final-existing-visual.log |
| 成果审核截图 | 1 passed，42.4s，比较 5 张，含两个新状态；axe 零违规 | r2-final-review-visual.log |
| R1 导航与退出截图 | 1 passed，26.3s，比较 4 张；axe 零违规 | r2-final-navigation-visual.log |
| 完整浏览器及 no-skip | 113 passed、29 已有明确豁免、0 unexpected、0 flaky，10.4min；no-skip 出口 0 | r2-final-full-browser.log、r2-final-full-browser-report.json |
| Linux 前端七项及类型契约 | 七项全部出口 0，641 单测；覆盖与 Windows 一致；原 audit 门禁按既有豁免通过，未扩充豁免；生成类型一致 | r2-final-linux-frontend.log |

752 个前后端文件已比较一致：文本仅归一化 CRLF，PNG 比较原字节；仅副本中的 playwright.config.ts / global-setup.ts 两处独占 PostgreSQL／Redis 地址适配排除。r2-source-tree-match.log、r2-frontend-hashes.json 将由最终回执再核对当前工作树。

## TDD 与独立评审

核心真实 PG 闭环先复现原即时发布入口不符合更新复审（RED 2），实现后 GREEN 2，扩展为 4 项覆盖退回、撤回、时间、固定快照、下架及历史标注。双创模块 148 passed、1 显式组合 smoke 未启用的跳过是中间验证；最终全量已启用对应组合测试。通知默认模板 RED 2 failed／2 passed → GREEN 4（11.23s），验证实际通知快照；历史和自定义模板不重写。前端状态／路由 RED 2 → GREEN 10。

一次新上下文评审范围 `3431b20..e1f0269`，原结论 REQUEST_CHANGES／With fixes。无 Critical；唯一 Important 是已下架成果的两处成功提示，真实浏览器 RED 4 断言 → GREEN 后提交 `b3d35d0`，随后重新执行受影响的完整前端门禁、浏览器与截图。没有第二次评审或伪造修复后 APPROVE。唯一延期 Minor 是新一轮待审时旧提交和旧批准决定的直接重放断言；矩阵 U2／G-6 如实记录，源码未发现状态破坏。详见 [最终评审与处置](innovation-update-review-final-review.md)。

## 环境修正与无效证据

- 初始全量后端与通知专项脚本固定同一测试库，造成重叠。已停止该次全量运行；r2-full-backend.log 和原覆盖文件无效，不计最终结果。通知专项改用独立 focus 库。
- 随后虽隔离 PostgreSQL，后端排名测试仍清理共用 Redis0，完整浏览器出现本人排名标记缺失。共享缓存的全量后端结果及失败浏览器结果均撤出最终证据，历史日志保留为 r2-final-full-backend.log / r2-shared-cache-browser-failed.log；不引用它们证明当前通过。
- 最终全量重新使用全新 campusquest_test_r2_isolated_20261010、Redis12 和新 coverage 文件；浏览器使用 campusquest_test_r2_browser_20261010、Redis13。缓存库使用前检查为空。未重置开发 PostgreSQL 或共享 campusquest_test。
- 开发 Redis0 排名派生缓存曾被误清，已只读现有开发账本、回滚 SQL session 后重建。r2-dev-ranking-rebuild.log：ranking:all 已重建，账本本来没有排名成员，写入 0 个成员；没有新建或重置项目、成果、账号、积分记录。不能声称缓存完全未受影响。
- 为修复 Important 停止旧源码的浏览器全量后，那个测试世界未完成 teardown，首次补充回归碰到旧成果。使用该次 world 文件清理其拥有的测试记录，然后才获得有效 RED；r2-down-copy-preparation-failed.log / r2-aborted-world-clean.log 保留。没有清空数据库。
- 排行专项的首次固定视觉标签准备错误，与缓存缺失原因不同；修正运行模式后 9 passed、1 已有条件豁免，r2-isolated-ranking-green.log。不改断言或扩充 skip 豁免。
- 原 npm audit 门禁在 Windows 报 spawnSync npm ENOENT，在 Linux 执行同一门禁。此前 Linux typecheck 与 Next dev 并发时生成文件临时缺失，不能算通过；最终前端门禁等待浏览器全部结束后串行运行。

## 体验环境与 Git

所有本地门禁完成。API 8200 / Web 3000 已恢复，三个原有演示账号均通过浏览、本人管理、权限限定运营入口和真实 POST logout 204；未修改开发内容或授权，日志 r2-dev-restore.log / r2-dev-demo.log。分支保持 codex/innovation-platform，现有 PR #43 保持 Draft，不修改 main、不合并。最终提交、fork 与 PR HEAD 的实时核验记录为工作区 r2-git-ci.json；完整 CI 必须查询最终 SHA 的 Actions 和 PR checks，GitGuardian 不能代替完整 CI。最终 task-done 回执会再次匹配当前源码、完整门禁标记、浏览器报告、干净工作树和实际远端提交。

## 实施裁决（按 ledger 顺序）

1. 不新增迁移，沿用唯一待审索引与不可变版本约束；若错，写入或不变量失败，须重新设计。
2. 使用实际矩阵与生成物路径，避免重复；若错，矩阵／契约漂移门禁失败。
3. 后端使用本轮独占测试库；若错，数据库保护拒绝或证据污染。
4. 浏览器另用独占测试库；若错，受污染证据必须作废重跑。
5. Windows audit 启动限制转 Linux 原门禁；若错，不能证明依赖审计通过。
6. 默认通知同步复审语义，保留历史与管理者模板；若错，批准通知仍可能误导。
7. 固定数据库脚本碰撞后作废初始全量；另用专项／最终库与新覆盖文件；若错，证据再次无效。
8. Redis 分离为后端12／浏览器13，撤回共享缓存回执并按原账本重建开发排名；若错，排名及验证仍可能受污染。
9. 完成的源码评审与隔离门禁并行，交付等待全部新鲜回执；若错，可能提前宣称通过，最终回执负责阻断。
10. 评审未判断的 R3、统一认证、批量历史重审继续留给后续；若错，路线图可能遗漏后续工作。
11. 评审未判断的管理员自定义通知措辞保留管理者配置；若错，自定义文案可能仍失准。
12. 评审未判断的草稿选择 URL 持久化保持既有行为；若错，刷新后需要重新选择草稿。
13. 评审未执行的完整门禁、缓存恢复和最终 CI 由实施回执核验；若错，会误报验证通过。
14. 下架回归不增加运营管理接口，准备程序只修改测试库中本 world 负责人拥有的已通过且无待审成果；若错，可能操作错误记录，提交前由测试库、归属和状态保护拒绝。
