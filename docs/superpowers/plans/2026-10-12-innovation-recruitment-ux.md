# 招募交互修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. 本轮沿用用户已确认的原生实施方向，由主代理实现，最终一次独立评审。

**Goal:** 修复用户走查确认的六项交互问题，让负责人能理解并顺畅完成草稿保存与预览发布。

**Architecture:** 保留现有 API、授权和公开版本规则。表单数据处理在 recruitmentForm.ts，界面与焦点在 RecruitmentEditor.tsx，项目初值由现有 Workspace 查询提供；不新增请求、依赖、数据库迁移或数据回填。

**Tech Stack:** 现有 Next.js 16.4 / React / TypeScript / 项目 Button、AuthField、Radix Dialog / Playwright。

**Spec:** ../specs/2026-10-11-innovation-recruitment-design.md 的 2026-10-12 交互修订；本聊天已确认的六项清单和“继续推进”授权。

## Global Constraints

- 保存不公开；发布与重新开放仍需负责人资格 AND 项目权限。
- 旧保存数据与公开版本不自动改写；联系渠道不默认授权、不读取账号绑定资料。
- 保留 codex/innovation-platform、现有开发数据和真实时钟；不改 main，不合并，PR 保持 Draft。
- 原生实施；本轮不开始 R4，不扩展成果审核、招募生命周期或学校认证。

## Review Focus

- 已保存的旧阶段说明与 idea_stage 保持可读，不因打开编辑器被自动改写。
- 新岗位带入项目初值时不能默认勾选联系渠道，不能把后续项目变更同步到公开岗位。
- 缺少发布必填项且有未保存改动时，字段错误优先，焦点到实际可操作控件。
- 未保存反馈必须在发布按钮所在视野内；成功、未知结果和跨账号清理仍保持原规则。
- 招募列表去掉冗余筛选时，项目库仍能筛选仅正在招募的项目。

## Task 1：项目初值与单一阶段入口

Files: recruitmentForm.ts、RecruitmentWorkspace.tsx、RecruitmentEditor.tsx、innovation-recruitment-form.test.ts、innovation-recruitment.spec.ts。

- [x] 写失败测试：新建岗位带入名称/方向/阶段说明/简介，阶段分类仍显式选择、contacts 为空；选择创意起步生成 stage=创意起步、idea_stage=true；切换其他阶段要求实际说明；旧 record 原样读取。
- [x] 在旧代码上运行并记录 RED。
- [x] 实现 initialRecruitmentForm(projectDefaults)、chooseRecruitmentStage(form, choice)；项目阶段选择创意起步或其他阶段，后者显示具体阶段说明。已有创意记录的原说明保留展示；切换比较两类选项分别保留并恢复临时说明，临时缓存不提交且不造成假 dirty。
- [x] 新建时以项目初值作为未修改基准；编辑既有岗位不重新带入项目资料。
- [x] 运行单测和对应浏览器回归，记录 GREEN。

## Task 2：保存、预览与确认反馈

Files: RecruitmentEditor.tsx、RecruitmentConfirm.tsx、innovation-recruitment.spec.ts。

- [x] 写失败浏览器测试：未保存时点击预览，提示在视野内；缺少字段且 dirty 时焦点到字段、校验摘要不被保存提示覆盖。
- [x] 运行旧代码并记录 RED。
- [x] 操作状态放到按钮旁；编辑输入清除旧保存成功消息；字段校验先于未保存检查，按阶段入口/具体联系输入或首个授权复选框定位错误，关联错误描述。
- [x] 编辑器说明保存草稿→预览→确认发布，保存不公开；确认按钮明确动作，保留原 Dialog 焦点和未知结果重试。
- [x] 更新既有招募 E2E 的阶段与确认按钮契约，运行回归。

## Task 3：浏览筛选、门禁与交付

Files: PublicProjectsView.tsx、PublicRecruitmentsView.tsx、e2e-selector-contract.md、test-matrix/innovation-recruitment.md、交付/走查文档、受影响的像素 PNG。

- [x] 招募列表省去仅看正在招募控件，项目库保留；通过真实页面和 E2E 核对。
- [x] 前端完整静态/类型/CSS/单测/覆盖/审计/构建/契约门禁；独立测试库跑完整浏览器电池与 no-skip。
- [x] 先比较原像素基线，确认变化只属于本轮批准的界面修复；受影响产品基线在独立提交更新，记录新旧走查，不放宽容差。
- [x] 一次独立最终评审，处理重要问题，重新验证最终产品源码；保留完整输出及源码身份。
- [ ] 保存并推送既有授权 fork，核对当前提交 CI 与 Draft PR；提供体验步骤，本轮到此交付。
