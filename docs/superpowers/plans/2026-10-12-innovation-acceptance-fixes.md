# 创新创业用户验收修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复用户已授权的深度验收报告 F01–F07，保持独立招募与联系授权业务边界。

**Architecture:** F01 使用同标签页、同认证 epoch 的内存恢复，站内离开后重新进入同项目自动恢复；不持久化联系值，不自动调用保存或发布。其他问题在招募功能的错误呈现、条件字段和焦点处理层修复，不修改共享系统错误映射、API、数据库或成果复审。

**Tech Stack:** 现有 Next.js/React/TypeScript、AuthField/Button/Radix、Node test、Playwright、真实独立 PostgreSQL 浏览器环境。

**Spec:** `docs/quality/innovation-user-acceptance-2026-10-12.md`（F01–F07 用户授权修复），业务约束参见 `docs/superpowers/specs/2026-10-11-innovation-recruitment-design.md`。

## Global Constraints

- 使用 `codex/innovation-platform`，不修改 main、不合并；PR #43 保持 Draft。
- 保存仅私有；发布/重新开放需明确确认；负责人资格 AND 项目权限由服务端验证；无需已有审核成果。
- 联系值仅主动读取；四渠道逐一授权，临时输入不写 localStorage/sessionStorage，不进入跨用户缓存。
- 成果更新完整复审，旧通过版保留至新版本批准；不开始 R4，不重置开发数据。
- 默认当前模型不切换；原生实施，末尾按代码评审技能要求一次独立评审。
- 同标签页恢复不承诺跨刷新/浏览器关闭；这类离开仍由 beforeunload 提醒。显式“返回并丢弃”清除内存快照。

## Review Focus

1. 退出、换账号、跨标签认证重置后不能恢复前账号的联系值；测试覆盖 epoch 清理与迟到回执。
2. 保存请求在站内离开后返回，不能恢复陈旧版本或自动重复命令；测试覆盖成功清理及未知请求恢复优先级。
3. 其他阶段的非适用目标/方案保留原输入、长度校验仍有效，不能偷偷改变后端字段；阶段切换往返测试覆盖。
4. 404 不暴露他人项目存在性，503 仍可重试；真实权限用例与只读故障用例继续覆盖。
5. 联系读取成功公告不自动读出联系值；返回、弹窗、重新读取焦点合理且不会制造未授权请求。

## Task 1: F01 输入恢复与隐私边界

**Files:** Create `frontend/src/features/innovation/recruitmentRecovery.ts`, `frontend/src/__tests__/innovation-recruitment-recovery.test.ts`; modify RecruitmentEditor.tsx、RecruitmentWorkspace.tsx、现有 `frontend/e2e/innovation-recruitment.spec.ts`。

**Interfaces:** consumes `getAuthEpoch/onAuthEpochChange`、既有未知命令恢复；produces `getRecruitmentRecovery(projectId)`、`rememberRecruitmentRecovery(projectId,snapshot)`、`clearRecruitmentRecovery(projectId,epoch,expectedForm?)`。快照含 epoch、record、form、newDefaults，不含额外 API 命令。成功回执只删除其拥有的不可变输入快照，避免迟到原保存清掉后来输入。

- [x] 写浏览器回归：输入岗位/联系值→站内创新创业→浏览器返回，自动恢复且没有保存请求；显式丢弃后无恢复；账号切换清理由认证 epoch 单测与现有角色切换浏览器用例共同验证。
- [x] 独立测试栈运行 `innovation-recruitment.spec.ts --grep 'R3 acceptance: recovery'`，旧实现失败于恢复输入断言，保留完整 RED 日志。
- [x] 实现内存恢复、认证变化清理、未知请求优先；成功保存清理对应快照，显式返回丢弃清理。表单请求期间锁定输入，迟到回执 compare-and-delete 不删除更晚快照。
- [x] 单测证明项目隔离、同账号刷新令牌保留、四类认证转换清空、旧 epoch 迟到写入不被接纳；定点浏览器和单测 GREEN。
- [x] 提交本任务源码/测试并记录 ledger。

## Task 2: F02–F07 提示与可访问交互

**Files:** modify RecruitmentWorkspace.tsx、RecruitmentEditor.tsx、RecruitmentContacts.tsx、recruitmentForm.ts、PublicRecruitmentsView.tsx；测试沿用 recruitment form/presentation 单测与现有 E2E；同步 test-matrix/innovation-recruitment.md 与选择器契约。

**Interfaces:** consumes Task 1 恢复；不改变 RecruitmentFieldsDto。联系错误按渠道返回可操作文案，仍使用原格式验证器；编辑器与预览共享条件字段规则。

- [x] 浏览器 RED：404 无重试且可返回本人项目；其他阶段不展示目标/方案且切回创意恢复；QQ 错误说明 5–20 位数字/非0开头；email/tel 类型与输入模式；键盘进入编辑器聚焦标题；读取成功 status 不含联系值；公开 false 标签“ 不接受零基础 ”；关闭无“下次提醒”。
- [x] 先跑定点回归和联系错误单测，当前行为失败，记录具体断言与日志。
- [x] 实现管理入口专属 404 提示；目标/方案仅创意起步展示和预览，保留临时值；按渠道错误及帮助/输入类型，移除重复错误；标题焦点与合理列表返回焦点；联系成功只公告“联系方式已读取”；零基础标签直接对应布尔值；仅 OPEN/NORMAL/无日期岗位显示下一次提醒。纠错控件保持到成功保存或显式切换阶段，不因第一个按键卸载。
- [x] 所有定点回归 GREEN；两端前端 typecheck/lint/check:css/unit/coverage/ratchet/audit/build（670单测）；真实浏览器全站139通过及 no-skip退出0；视觉先比旧基线，逐图解释批准内容变化后仅更新受影响的三张招募 PNG，独立提交，不改容差/字体；随后20视觉用例独立比较通过。
- [x] 独立评审最终修复范围；两项重要发现再 RED→GREEN，重跑受影响门禁。457f09b 定点复审 APPROVE，证据见质量回执，不引用历史通过数。
- [ ] 浏览器重新按用户流程验收 7 项并保留模拟记录关闭状态、开发服务；写交接/验证回执、提交推送，核对当前提交完整 CI 状态与 Draft。

## 计划自检与执行记录

范围覆盖 F01–F07；界面大改、存量演示资料正文、列表上下文等报告中的后续建议不在本批。Task 2 的 Editor/Workspace 与 Task 1 相交，顺序执行，避免相互覆盖。用户“修复”已授权实施，复用已确认方向，原生执行，不重复请求确认。源码起点 `c4e2e043bc88a4f678a490ba7ba3c6b773d581f4`。
