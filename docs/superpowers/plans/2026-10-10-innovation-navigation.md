# R1 Navigation and Logout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. 当前用户已授权本会话连续实施，复用已确认方向，不再询问执行方式。

**Goal:** 交付一级创新创业入口、本人/运营分流及真实退出，可供本轮体验。
**Architecture:** 复用 StudentShell、校内成果列表、服务端能力接口与 logout；独立退出页卸载私密表单，成功整页进入登录。无后端/API/数据库改动。
**Tech Stack:** 安装版本的 Next.js/React/TypeScript、现有 Dialog/Button、Node 纯测、Playwright、真实 PostgreSQL 演示测试世界。
**Spec:** `docs/superpowers/specs/2026-10-10-innovation-navigation-design.md`

## Global Constraints

- 在 `codex/innovation-platform` 上从 `971b16c` 开始；不重置开发数据、不改 main、不合并，PR 保持 Draft。
- 保留三库范围与教师/管理员 ACTIVE 校内浏览；无新依赖，不改变后端鉴权。
- R2/R3/SSO 不在本轮；模拟资料标注，完整 CI 对准最终 head。
- 导航使用既有 tokens；手机五栏+全导航菜单，受限资料不共享缓存。
- Windows 使用 npx.cmd；专用测试库不使用开发库/共享测试库；记录完整输出与退出码。

## Review Focus

- 公共入口和运营路由前缀重叠：每个导航 landmark 最多一个 current。
- 能力加载失败或授权撤回：不默认显示运营，不绕过后端拒绝。
- 退出超时/5xx/丢响应：停在确认页，允许重试，禁止声称成功或重新展示旧草稿。
- 退出后后退/刷新/换账号/跨标签：旧私密内容不得复活。
- 异步确认导致按钮消失/禁用：稳定目标接收焦点；epoch 切换不恢复旧账号焦点。

### Task 1: 导航与入口分流

**Files:** StudentShell、TeacherShell、AdminShell、BottomNav、WorkspaceSidebar、StaffMenuSheet；`features/innovation/PublicAchievementsView.tsx`、`InnovationNavigation.tsx`；新 `(campus)/innovation/page.tsx`；profile/page.tsx；navigation 单测与 e2e。
**Interfaces:** Consumes `useSession()`、`getInnovationCapabilities()`、`PublicAchievementsView()`、SideNavItem。Produces `/innovation` 和授权运营导航；SideNavItem 增加可选 exclude 前缀，navItemActive 采用路径段边界匹配。

- [ ] Step 1: 写导航匹配纯测和真实浏览器入口/手机/权限测试；矩阵先写预期。
- [ ] Step 2: 运行纯测与浏览器测试，确认缺少入口/重叠 current 的 RED。
- [ ] Step 3: 实现分流，旧 URL 保留；三库尚未实现部分只说明，不假按钮；沿用学生壳与 staff 入口。
- [ ] Step 4: 纯测、typecheck/lint/CSS 和相关浏览器测试 GREEN。
- [ ] Step 5: 提交 `feat: separate innovation browsing and workspace navigation`。

### Task 2: 真实退出与本轮焦点修复

**Files:** 新 `(auth)/logout/page.tsx`、`features/auth/LogoutView.tsx`；AccountSettings；各壳账号区域；ReviewConfirm、AchievementReviewsView、AchievementReviewPanel；navigation e2e 和 focus 纯测。
**Interfaces:** Consumes `logout(): Promise<void>` 与 `getAuthEpoch(): number`；Produces 退出页，`ReviewConfirm` 可选 `fallbackFocus` ref。

- [ ] Step 1: 写真实注销/刷新/换账号/跨标签和 503→重试测试；审核成功/不确定结果焦点回归。
- [ ] Step 2: 运行观察 RED（入口/焦点目标尚缺）。
- [ ] Step 3: 退出页真实调用并失败留页；成功整页 replace。确认框恢复当前 epoch 的原按钮或稳定 fallback。
- [ ] Step 4: focused tests GREEN，再运行完整前端单测与静态门禁。
- [ ] Step 5: 提交 `feat: add real logout and stable review focus recovery`。

### Task 3: 验证、独立评审与体验交付

**Files:** test-matrix/innovation-navigation.md、e2e selector contract、新 navigation visual spec/豁免、新 PNG 与有意变化旧 PNG；docs/demo/innovation-navigation.md、verification 与 handover。
**Interfaces:** Consumes Task 1/2 成品和当前提交；Produces 真实门禁记录、验收指南、Git/CI 状态。

- [ ] Step 1: 运行完整前端 unit、coverage ratchet、typecheck、lint、CSS、audit、build，退出码明确。
- [ ] Step 2: 专用库运行完整浏览器电池及授权/切换专项；Linux 固定字体像素/axe，失败先查因，不放宽门槛。
- [ ] Step 3: 有意产品变化按新旧截图逐图走查，基线独立 commit；新增选择器/豁免/矩阵同 ship。
- [ ] Step 4: 独立只读 reviewer 按 R1 spec/plan 评审本轮起点至成品提交。调用前说明模型；重要项 RED→GREEN 修复，记录取舍。
- [ ] Step 5: 保存验收/验证/交接，推送授权 fork 分支，核实 Draft PR #43 的最终 head 与完整 CI；恢复本地体验服务。

## 自查与执行记录

三项任务覆盖当前 R1；不重复已完成的成果审核实现。Task 2 依赖 Task 1 的账号区；Task 3 依赖全部路由和 fallbackFocus，命名一致。详尽日志在仓库外 `.local-dev/logs`，本计划执行状态随实际证据更新。
