# R1：导航与退出验证记录

2026-10-10，起点 `971b16cb35d6faba9a6dae5503148ca264a0d247`，分支 `codex/innovation-platform`。用户授权只交付 R1；既有成果更新免复审行为由后续 R2 替换，本轮不变更后端业务状态、接口或迁移。

## 交付范围

设计和计划分别为 `docs/superpowers/specs/2026-10-10-innovation-navigation-design.md`、`docs/superpowers/plans/2026-10-10-innovation-navigation.md`。成果浏览在一级“创新创业”；本人维护在“我的”；运营入口仅在 ACTIVE 学生获服务端授权后显示。人才／导师尚未开放文字不生成假路由。手机五栏与全部导航保留既有奖励等目的地；教师／管理员有校内浏览及返回各自工作台路径。

退出确认页先卸载私密工作区，真实注销成功后整页进入登录；失败不假成功。精确 `401 AUTHENTICATION_REQUIRED` 在该注销端点表示没有 refresh credential，可结束退出；一般 401、CSRF 拒绝、5xx 与网络故障不能冒充成功。

## 独立评审与修复

一次 Astra High 只读评审 `971b16c..699b45e`，结论 REQUEST CHANGES：无 Critical，三项 Important，无 Minor。并非最终 HEAD 的 APPROVE，也不是合并授权。

1. 退出无响应时卡死：新增 15 秒取消预算，覆盖 HTTP 请求和前置 refresh drain。超时不抢先发注销；旧 refresh 继续被跟踪，下次重试仍须等待它，保持 cookie 写入顺序。三项单位回归先 RED（3 失败／3 通过）再 GREEN（6 通过），含 drain 超时及后续安全重试。
2. 子路径部署退出跳到根登录：应用前缀来自构建的 `CQ_BASE_PATH`，显式暴露给浏览器，与 API 前缀独立；整页跳转保留。`app-path.test.ts` 复现 `/campus` 前缀丢失再通过。
3. 原请求重试成功后焦点丢失：负责人原请求重试 RED，焦点落在不可操作区域；修复后运营重试也 RED，再补两侧完成后 useLayoutEffect 的稳定回焦。`r1-review-focus-red.log`、`r1-review-ops-focus-red.log` → `r1-review-browser-green.log`，真实三账号审核闭环和无响应退出 2/2 PASS。仅当前 epoch、已连接且可操作的目标接收焦点；处理中重新读 workflow 不阻止冻结请求重放。

## 验证环境与证据

完整输出保存在工作区外 `C:/Users/111/Desktop/华师令/.local-dev/logs/`。前端 Node 22.23.3；Linux Ubuntu／固定 Noto Sans CJK SC 和 Liberation Mono；真实 PostgreSQL、Redis、HTTPS MinIO、ClamAV。独占库 `campusquest_test_r1_20261010`，world 仅清理自己的演示对象，开发库及共享测试库未重置。Linux 只把 Playwright config/global setup 的本地测试库名称改为独占库，不改产品配置。

| 验证 | 当轮日志 | 结果 |
| --- | --- | --- |
| 最初导航 RED | `r1-navigation-red.log` | 一级入口不存在，出口 1 |
| 最初退出／确认焦点 RED | `r1-logout-focus-red.log` | 两个退出入口及异步焦点失败，出口 1 |
| 专项首次 GREEN | `r1-focused-green-2.log` | 8 通过，出口 0；不替代评审修复后的电池 |
| 评审修复单位 RED/GREEN | `r1-review-unit-red.log` / `r1-review-unit-green.log` | 3 失败／3 通过 → 6 通过 |
| 评审前全浏览器 | `r1-full-battery.log` | 112 通过、29 明文豁免，出口 0；不替代修复后验证 |
| 最终完整静态、单位、覆盖、审计和构建 | `r1-final-typecheck.log`、`r1-final-lint.log`、`r1-final-css.log`、`r1-final-unit.log`、`r1-final-coverage.log`、`r1-final-audit.log`、`r1-final-build.log` | 全部出口 0；640 单测无跳过；覆盖 lines/branches/functions 95.46/91.56/90.43%，原下限 95.21/91.22/90.19% 不变；审计 5 项既有开发链豁免不变，无未豁免 high/critical |
| 最终完整浏览器及 no-skip | `r1-final-battery.log`、`r1-final-battery-report.json`、`r1-final-no-skips.log` | 113 通过、29 明文豁免、0 失败，10.6 分钟；105 Chromium + 8 Firefox/WebKit 窄 smoke；浏览器命令与全部 spec no-skip 检查均出口 0 |
| 最终身份 fence / refresh wave 专项 | `r1-final-auth-fence.log`、`r1-final-auth-fence-report.json` | 3 通过、0 跳过、46.9 秒，出口 0；旧身份迟到响应不重绘，显式切账号隔离，一轮只有一次 refresh，两标签均恢复登录 |
| 固定字体像素、axe、新旧走查 | `r1-visual-final.log`，`innovation-navigation-visual-walkthrough.md` | 原16 + 审核3 + 新R1 3，22 PNG 不更新比较 PASS；后两场景 axe 0；17 张历史有意更新逐图走查，新3首建后重新比较，出口 0 |
| 规约对账检查器 | `backend/scripts/check_test_matrix.py` | 14 个矩阵 OK，出口 0；R1 IN01–IN09 均有真实测试引用 |
| 子路径生产构建与导航 | `r1-prefixed-build.log`、`r1-prefix-navigation.log` | CQ_BASE_PATH=/campus 构建 PASS；实际 /campus/logout→/campus/login PASS（退出 transport stub，仅证明导航；注销真实性由完整浏览器另证） |
| Linux / Windows 源文件一致性 | `r1-tree-verify.py` | 322 个前端文件匹配：文本归一化 LF，PNG 精确 hash；仅两份独占测试库 harness 排除。后续文档提交不改变已测产品树 |
| 本机已有演示账号 | `r1-local-demo-probe.log` | API 8200 / Web 3000 恢复；20269003、20269002、20269001 全部实际登录、浏览、本人管理及真实 logout 204 PASS，出口 0；未修改内容/授权或重建资料 |
| 当前提交远端 CI | `.local-dev/logs/r1-final-git-ci.json` 和实时 PR | 发布回执记录最终本地/fork/PR head、Draft 与同提交 Actions/检查状态。完整远端 CI 尚无通过证据，GitGuardian 不等于完整 CI；不可宣称可合并 |

## 实施取舍与代价

记录顺序沿用执行 ledger；六项裁决完整保留。本轮无 Minor，故无延期 Minor 清单。

- Task 1＋2 合并为一次产品提交：共享账号导航与退出的壳修改，保证原子交付。代价：需要独立回退时须拆分提交。
- R2、R3、人才／导师完整业务和学校认证延期：用户明确分阶段授权。代价：R1 仍不能演示完整双创流程。
- 新截图及最终 CI 工件由执行代理完成，评审不提前将未完成工件判为缺陷。代价：须以最终实际门禁补证据，不能借评审替代。
- 授权撤回不新增实时推送：菜单在重新读取／刷新后更新，后端每次操作仍鉴权。代价：已打开页面可能暂时保留过时菜单，但不能越权操作。
- 进入退出确认页即卸载未保存表单：按 R1 设计保护私密工作区。代价：取消退出也不会恢复未保存输入，验收指南已说明。
- 共用壳合并提交的评审取舍沿用第一项，不要求拆分或重置数据。代价同第一项。

## 体验与下一阶段

验收见 `docs/demo/innovation-navigation.md`。交接见 `docs/handovers/2026-10-10-innovation-r1.md`。本轮完成后停在 R1，等待用户体验；R2 全部已公开成果内容更新复审、审核期间旧版保留仍是下一阶段已确认规则。

## 实际门禁命令

Windows 前端分别执行 `npm.cmd run typecheck`、`lint`、`check:css`、`test:unit`、`coverage:ratchet`、`build`；审计在 Linux 执行 `npm run audit:gate`，避免 Windows npm 子进程解析限制。全部保留完整输出及实际退出码，不以截断摘要替代日志。

Linux 完整浏览器：`CQ_E2E=1 node node_modules/@playwright/test/cli.js test`；报告保留为 `r1-final-battery-report.json`。随后 `node scripts/assert-e2e-no-skips.mjs` 检查全部 spec 的明文豁免，视觉独立阶段不由功能电池的 skip 冒充通过。

执行包装脚本首次将后续 no-skip 写成不存在的 `e2e/check-no-skips.mjs`，导致包装出口 1；浏览器本身已出口 0。按上面仓库真实路径单独重跑检查出口 0，完整日志保留；没有更改测试或放宽豁免来处理该命令路径错误。

会话专项：`CQ_E2E=1 CQ_E2E_FENCE=1 CQ_E2E_PROBE=1 node node_modules/@playwright/test/cli.js test auth-cross-tab-fence.spec.ts auth-wave-invariant.spec.ts --project=chromium`。专用测试库适配与生产源一致性按上表说明。

视觉三个独立 world 分别运行 `visual-regression.spec.ts`、`innovation-review-visual.spec.ts`、`innovation-navigation-visual.spec.ts`，显式 `CQ_E2E=1 CQ_VISUAL=1 CQ_E2E_FIXED_LABELS=1`。最终比较不带更新参数。CI YAML 用已安装 js-yaml 解析，三项 jobs 及新 R1 视觉阶段存在；解析成功并不代表远端运行成功。
