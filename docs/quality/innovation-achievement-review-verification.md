# 成果核实检查点：验证与决策记录

日期：2026-10-09。对应 `2026-10-09-innovation-achievement-review.md` 计划，分支 `codex/innovation-platform`，Draft PR #43。此记录在最终门禁执行中维护；未完成项明确标出，不能将已有子集结果当作完整完成证据。

## 业务范围与验收

两份原始资料再次核对：双创独立需求 v0.7 的 PR-06/08/11/12、OP-01、AC-01，以及技术实现文档 v1.1。技术建议中的代码假设以仓库实际实现为准；附件不是操作授权。

本轮负责人上传私密证明并提交不可变快照，运营领取后核实或有理由退回，通过后向正常校内登录用户展示白名单内容。保存草稿不改变公开版本；作者显式发布已通过成果的更新，不创建复审，不改变首次核实日期，不恢复下架。首次资格四项申请与成果核实分开。

本轮不构成完整三库交付。招募与授权联系方式、人才库、导师库、共同管理/成员确认、全套举报治理与激励仍需后续计划。预置开发账号不等于学校统一认证；统一认证协议与身份映射须学校确认。

本地验收的三个新演示账号与约十分钟路径见 `docs/demo/innovation-achievement-review.md`；原开发资料未重置。示例项目和 PDF 明确标注演示用途。

## 验证日志索引

完整原始输出位于工作区 `C:/Users/111/Desktop/华师令/.local-dev/logs/`，不提交运行日志、覆盖率数据库、签名 URL 或私密上传字节。权威覆盖率为 Linux / Python 3.12，collector 同时开启 greenlet/thread；前端使用 Node22.23.3、Ubuntu24.04 与固定字体。

| 门禁 | 日志 | 本轮状态 |
| --- | --- | --- |
| 后端格式、Ruff、mypy、矩阵、单元/worker、真实集成、覆盖棘轮 | `review-linux-backend-final.log` / `review-linux-final-coverage.json` | 411文件格式、Ruff、mypy192、13矩阵通过；1307单元/worker及1218集成通过（7项非integration取消选择）；全部出口0，双创98.6高于原97.8地板，其余地板不变 |
| 前端类型、lint、CSS、覆盖棘轮、依赖审计、生产构建 | `review-frontend-post-preparation.log` | 测试准备修复后重新执行出口0；95.44/91.61/90.51，原地板95.21/91.22/90.19不变 |
| 前端纯测 | `review-frontend-unit-post-preparation.log` | 633通过、零失败/跳过，出口0 |
| 完整浏览器及跨引擎烟测 | `review-browser-battery-final.log` / `review-browser-battery-final-report.json` | 105通过、零失败、28明文豁免，8.6分钟，出口0；99项Chromium与6项Firefox/WebKit基础烟测，不声称双引擎全部成果功能覆盖 |
| 浏览器准备回归 | `review-battery-preparation-green.log` | 2通过、1.0分钟；等待首页有限动画结束，并以真实管理员API准备成果测试状态，不改变登录限流 |
| 不意外跳过自证 | `review-browser-noskip-final.log` | 出口0，遍历全部spec，仅28项明文豁免；独立视觉测试另跑 |
| 新页面3张PNG及axe | `review-new-visual-post-preparation.log` | 最终测试准备修改后新鲜比较1通过、37.1秒；三页axe无违规，无更新开关 |
| 既有16视觉场景 | `review-existing-visual-post-preparation.log` | 最终测试准备修改后新鲜比较16通过、1.3分钟，无更新开关 |
| 真实三账号成果闭环 | `review-browser-503-green.log` | 1通过，含提交真实提交后503、决定真实提交后网络ACK丢失及同浏览器跨标签账号隔离 |
| 上传恢复回归及成果闭环 | `review-upload-reset-red.log` / `review-upload-reset-green.log` | 真实PUT403→移除意向后RED缺少放弃入口；修复后1通过59.4秒，重新选择、再次上传及整条核实流程通过 |
| 后端依赖审计 | `review-backend-audit.log` | 出口0，No known vulnerabilities found |
| 后端e2e | `review-backend-e2e.log` | CQ_E2E=1，42通过、147.86秒，出口0；含完整期限、并发、隐私、排行恢复与worker重试 |
| CI真实扫描组合准备 | `review-ci-scanner-ready.log` / `review-ci-evidence-smoke.log` | 相同compose命令等待健康成功；6项真实Clamd/PG/MinIO/生产provider通过、5.65秒，出口0；仅本地暖服务，不声称远端冷启动已通过 |
| 最终独立评审 | 完成后记录具体提交范围及结论 | 待结果 |

新增 OpenAPI 快照、生成类型、规约矩阵、选择器契约、no-skip 豁免和PNG随本PR提交。前后端均不降低覆盖地板或放宽视觉阈值。

验证树与提交树校对：425个后端文件与最终后端测试archive的SHA-256一致；289个前端源文件、测试、PNG及生成契约与实际Linux验证树一致。仅排除明确改成独占库的Linux全局setup/config这两处本地harness适配。最终矩阵的新行按已有`tests/../../frontend`引用协议校正，checker未改变，13矩阵重新通过。

首次全量后端1307单元/worker+1185集成虽通过，双创覆盖95.1低于97.8，原始失败保留在 `review-linux-backend-full.log`。新增真实PG/HTTP拒绝与重放边界后，双创诊断子集188通过、98.6，仅用于定位。随后全量重新采集1307+1218，最终双创98.6且18个模块原地板均通过；不拼接不同collector配置或借旧数冒充最终结果。集成输出保留29条警告，主要为既有任务validation-worker测试的连接回收及上游弃用警告，不声称警告为零。

## 实施决策与代价

1. 继续已有隔离功能分支，而不另建worktree。它已有获授权fork与Draft PR且执行串行；错误代价是checkout并发，提交前核对状态。
2. 集成及浏览器分别使用独占测试库，不清理开发库/共享测试库。代价是额外两个测试库，避免跨运行污染。
3. 扫描异常采用仓库Ruff要求的Error后缀。业务语义不变；代价是调用方须导入规范名称。
4. 10MiB真实扫描烟测用末尾含独立EICAR的stored ZIP，而非给EICAR任意拼接字节。实测原夹具不能代表病毒特征；代价是可能误读为ZIP上传许可，API仍只接收PDF/PNG/JPEG，此烟测不声称完整解析安全。
5. Alembic缺少已有生成模板，使用produce_migrations/render_python_code生成并审阅新迁移，依赖unique先于FK；不修改已分享历史。代价是需人工审阅生成排序，0030–0032各自往返及模型对账已执行。
6. Linux浏览器暂借本会话拥有的3000，核对父进程归属后仅停止拥有的Web，finally恢复。代价是本地预览短暂中断，未采纳不明进程或清理开发资料。
7. 网络错误及5xx均保留提交/决定的原请求键和冻结载荷。真实服务可能已提交但ACK丢失；代价是用户需明确重试原操作，避免产生重复版本/决定。
8. 历史引用证明不占五份未引用上传槽位；每版选择仍限1～5份，历史材料不能移除。代价是随真实版本增加历史存储，不把首次五份限制变为终身五份。
9. 仅同步上游新增第五任务造成的旧任务PNG，并用卡数5/页脚5/5强断言约束，再更新本计划改变的成果草稿公开说明PNG。新旧图均走查；代价是错误夹具可能被认可，强内容断言与完整电池提供检查；不增遮罩、不放宽1%像素比例。
10. 成果测试通过已确认管理员的真实API准备资格和运营授权，管理界面由既有专门spec覆盖；负责人、运营、浏览者仍走真实页面。代价是单个成果spec不再单独证明管理UI，须完整电池包含那两组spec；未绕过或放宽生产登录限流。
11. CI与release命令显式启用真实扫描组合；新视觉spec在独立world执行，避免授权状态改变旧负责人截图。代价是冷启动需在360秒内取得官方病毒库，另一次视觉启动增加耗时；本地健康与真实组合已通过，远端CI结果另行核验。

视觉细节、来源提交与基线分类见 `innovation-achievement-review-visual-walkthrough.md`。最后的代码评审发现、修复及延期项在完成后追加；当前未声称GitHub CI或合并批准。
