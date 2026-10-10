# 双创 R1 交接：导航与真实退出

2026-10-10。本轮沿用 `codex/innovation-platform`，从干净 `971b16cb35d6faba9a6dae5503148ca264a0d247` 开始，只实施总规划 R1。用户接下来先体验，未授权本轮连续推进 R2。

## 阅读顺序和真源

1. 工作区和仓库 AGENTS.md，仓库质量文档。
2. `2026-10-10-innovation-roadmap.md`：全局规划与用户确认；本文件是最新 R1 实施回执。
3. `2026-10-09-innovation-continuation.md`：历史实现与验证索引，历史检查点不能证明本轮。
4. `docs/superpowers/specs/2026-10-10-innovation-navigation-design.md`、对应 plans 文档：R1 细化设计/计划。
5. `docs/quality/innovation-navigation-verification.md`、test-matrix/innovation-navigation.md、visual-walkthrough.md：本轮验证。
6. `docs/demo/innovation-navigation.md`：用户验收指南。

## 已实现及保留的业务边界

- `/innovation` 一级“创新创业”：复用真实校内成果浏览；项目库可达，人才／导师明确尚未开放；演示资料及未接 SSO 提示。
- “我的 → 本人双创管理”：我的项目、负责人资料与资格。原 URL 保留。
- “双创运营工作台”：仅 ACTIVE 学生具备服务端运营授权时显示；后端鉴权仍负责拒绝越权。授权变化重新读取/刷新更新菜单，不新增推送机制。
- 学生手机五栏和全导航；教师/管理员可进入校内浏览并返回自己的工作台，ADMIN 返回 `/admin/users`。
- 账号区域及账户设置真实退出入口。独立 `/logout` 先卸载旧私密工作区；成功后整页替换到登录，保留应用 basePath。15 秒覆盖请求和旧 refresh drain；超时显式重试，旧 refresh 仍被跟踪，不能越过它发注销。503/网络失败不假成功；仅该端点精确无 refresh credential 的 401 可结束退出。
- 审核确认、负责人/运营原请求重试的异步焦点修复，同 epoch 的稳定结果/重试目标接收焦点，避免已消失/禁用按钮与旧账号回焦。
- 未更改后端业务状态、接口、迁移、开发资料；不重置开发库或共享测试库。

已公开成果更新仍是历史免复审实现，**R2 尚未实施**。后续必须改为所有已公开成果内容更新复审，待审保留旧通过版，新版通过才替换。招募独立免审，但须负责人资格与对应项目权限；不依赖已有审核成果，不能放宽成所有人可发。学校统一认证最后接；默认 Sol High，模型切换需任务开始前说明。

## 本轮保存与核验

产品提交：`6c69897`（细化设计/计划）、`699b45e`（导航/退出/焦点及回归）、`c6247bc`（最终独立评审的 3 Important 修复）；截图独立 `25b9a84`；视觉/axe/CI 工件 `b83bbe6`。最终文档提交的 SHA 以 `git rev-parse HEAD` 及 PR head 为准，避免在文档自身循环填写 SHA。

本轮一次 Astra High 只读评审得出 REQUEST CHANGES：3 Important、0 Critical/Minor。三项已 RED→GREEN 修复，不冒充最终 head APPROVE。门禁结果与远端提交核验见验证记录；完整 CI 必须对准最终提交，GitGuardian 单项不代表完整 CI。

修复后本地门禁：640 单测无跳过；113 浏览器通过、29 明文豁免、0 失败/flake，no-skip PASS；额外 3 身份 fence/wave 专项无跳过 PASS；22 PNG 不更新比較 PASS；类型、lint、CSS、覆盖率、审计、普通/子路径生产构建及 14 份规约矩阵全部 PASS。已有三个开发演示账号实际导航和 logout 204 试走 PASS；服务已恢复。

保持 [PR #43](https://github.com/Penghuige/campus-quest/pull/43) Draft；推送目标仍是授权 fork `YeZesen0121/campus-quest`。不修改 main、不合并、不转 Ready。

## 本地体验与环境

入口 `http://127.0.0.1:3000/login`。负责人 `20269001`、运营 `20269002`、普通浏览者 `20269003`，密码 `student-demo-2026`。已有资料保留，按真实当前状态体验。指南包含五分钟步骤、退出/切账号、手机菜单及未保存输入说明。

Windows 8000 处于系统排除范围，仓库外 `.local-dev/Start-Dev.ps1` 与 Set-DevEnvironment.ps1 使用 API 8200；Web 3000 代理到它。仓库产品默认端口及 CI 配置不为本机例外改写。用启动器 All 恢复；日志在 `.local-dev/logs/`。演示服务启动不等于重建演示数据。

Linux 浏览器使用独占 `campusquest_test_r1_20261010`；只有 Playwright config/global setup 在容器内指定此库。322 个前端文件与 Windows 待交付源树 hash 一致（文本 LF 归一、PNG 精确）；测试库适配文件单独排除。固定字体基线留在仓库，完整执行日志、对比图及报告在仓库外。

## 下一轮起点

先收用户 R1 体验反馈，不自动补全三库。若用户继续授权 R2，先更新成果更新的规格、状态/版本边界和规约对账，再按真实 PostgreSQL 并发、旧版保留与权限证据实施；旧验收指南第五步不得继续作为目标行为。
