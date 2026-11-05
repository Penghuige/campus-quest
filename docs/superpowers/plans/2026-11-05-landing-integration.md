# Landing 真实接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans inline task-by-task. Steps use checkbox syntax.

**Goal:** 将Q14定稿Landing接入真实Next前端与登录，按Q15保留工作台，推送独立分支。
**Architecture:** public/landing同源隔离运行文档；根页基于既有session evidence分流，StudentShell权威判定；/about公开；原认证路由不改。
**Tech Stack:** 项目锁定Next16.4.2/React19.3/TS，原生ESM/WebGL及已有本地vendor。
**Spec:** docs/superpowers/specs/2026-11-05-landing-integration-design.md

## Global Constraints
- 基线4876594，分支codex/landing-production，保留原脏checkout和v1。
- 用户本轮要求已授权实现与push；Q15已选路由，沿用inline，不额外要求重复审批。
- 无新运行依赖、无后端/数据库/认证规则改动。所有登录走既有/login，支持CQ_BASE_PATH。
- 每任务RED→GREEN、完整证据写.local-dev/landing-integration/ledger.md。一次最终独立全量评审。

## Review Focus
1. 过期或伪造cookie不能绕过useSession或泄露学生内容；根页anonymous回Landing，受保护页仍跳登录。
2. 两个入口、回退与键盘必须跳到顶层/login，带部署前缀且不能跳外部origin。
3. iframe与主站不串CSS/滚动/历史资源；卸载/换页释放页面，手机/reduced/noGL不困住访客。
4. 稳定首屏/新印面忠于Q14，素材无漏项且错误响应不伪装HTML成功。
5. lint/CI不误扫vendor，不借忽略/刷新旧baseline掩盖第一方代码或旧工作台回归。

### Task1: 公共运行文档与真实入口
- [x] 先加runtime完整性与同源/前缀/顶层入口失败测试，运行观察RED。
- [x] 复制80项到public/landing，记录来源；加入共享真实登录导航、清理演示和工具面板，保留正常视觉。
- [x] 资源清单/语法/导航单测GREEN；单独记录已修改第一方路径，vendor只对lint作有依据忽略。

### Task2: 根路由与工作台组合
- [x] 路由/SSR/过期cookie失败测试，观察RED。
- [x] 根页移出学生组；复用StudentShell，新增根页专用anonymousFallback；/about与有标题全屏iframe及无JS入口。
- [x] 保留登录/角色/受保护路由，支持basePath，metadata按访客/工作台区分；针对测试GREEN。

### Task3: 验证、证据与推送
- [x] 加Landing真实Next e2e、稳定视觉/axe、资源/入口/手机/reduced/noGL/前缀检查；只更新被Q15取代的匿名根页旧期待。
- [x] 同PR更新测试矩阵、选择器与新视觉工件；运行类型/lint/CSS/架构/单测/覆盖/构建与相关e2e，说明未能完成的环境门禁。
- [x] 一次独立全量评审，重要项一次RED→GREEN修复；文档与事实一致。
- [ ] 明确exitcode核验pre-push，提交、push到origin并核对远端SHA；创建PR并跟进本次CI，不合并。
