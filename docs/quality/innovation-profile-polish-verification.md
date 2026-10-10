# R1 体验精简：退出文案与账号图标

2026-10-10，起点 `8ae2a5ff60d89934ba6ccfddaa8ea38c84505327`，沿用 `codex/innovation-platform`。依据用户两张截图的明确要求，仅修改两处展示：删除“我的”底部退出按钮前的可见标题／说明；学生顶栏账号入口复用现有 UserIcon 线条人像，替代昵称首字。入口的目的地、可访问名称、确认与真实注销均保持原契约；不改变后端或开发数据，不实施 R2。

## 验收与新跑证据

刷新 `/profile`：右上角圆形入口是人像，点击进入“我的”；下滑到底只显示“退出登录”按钮。切到“个人信息”也应如此。点击后仍进入独立确认页，确认成功回到登录页；失败保留重试。

复用已有本机演示探针，先在原实现观察可见标题仍存在而失败（`r1-polish-red.log`，出口 1）；修改后三个预置账号验证标题／说明消失、账号入口 SVG 存在、两分区的退出位置和实际 logout 204（`r1-polish-green.log`，出口 0）。没有为这次可逆展示调整新增单元测试或业务测试套件。

完整日志在仓库外 `C:/Users/111/Desktop/华师令/.local-dev/logs/`。Node 22.23.3；Linux 固定 Noto Sans CJK SC／Liberation Mono，真实 PostgreSQL、Redis、HTTPS MinIO、ClamAV，沿用独占 `campusquest_test_r1_20261010`。只准备和清理 browser world 自身对象，不清空开发库或共享测试库。

| 本次验证 | 完整日志 | 结果 |
| --- | --- | --- |
| 类型、lint、CSS、单位、覆盖、构建 | `r1-polish-{typecheck,lint,check-css,test-unit,coverage-ratchet,build}.log` | 全部出口 0；640 单测通过，0 跳过；覆盖 95.47/91.56/90.43%，原下限不变 |
| 导航与浏览器专项 | `r1-polish-navigation.log`、`r1-polish-navigation-report.json` | 14 通过，0 跳过／失败／flake，2.3 分钟；Chromium 导航 6 + Firefox/WebKit 窄 smoke 8；含真实注销、失败／超时重试与跨标签隔离 |
| 固定字体最终像素比较 | `r1-polish-visual-regression-final.log`、`r1-polish-innovation-review-visual-final.log`、`r1-polish-innovation-navigation-visual-final.log` | 原 16 + 审核 3 + 导航／个人页 4 = 23 PNG 无更新参数比较通过；审核／导航场景 axe 0；实际手机底部按钮在固定导航上方 |
| 规约对账 | `r1-polish-matrix.log` | 14 份矩阵通过；新增 IN11 由用户截图要求推导，复用已有视觉／导航证据，选择器契约同步 |
| Linux／待交付前端源 | `r1-tree-verify.py` | 323 文件匹配（文本归一 LF，PNG 精确）；只排除两份独占测试库 harness 配置 |
| 恢复后的本机演示 | `r1-polish-restored-demo.log` | API 8200／Web 3000 恢复；三个已有账号验证图标、精简文案、两分区位置及实际 logout 204，出口 0；未修改内容或授权 |

本轮依照前端视觉改动的 A 类语义面门禁运行导航专项；没有重跑全量浏览器电池、全部 spec no-skip 检查、独立身份 fence／wave、子路径构建或依赖审计。测试源、跳过豁免、依赖、注销逻辑和部署路径均未修改；历史门禁仅见前两份 R1 回执，不冒充本次新跑。

## 基线走查与保存

从起点逐图对比全部 34 张历史 PNG：16 张有意更新，18 张字节不变。14 张桌面差异仅为人像字形，bbox `(1392, 29)–(1406, 45)`；手机菜单仅图标变化，bbox `(277, 22)–(291, 38)`，其他正文／尺寸像素一致。个人页 390×844 视口同时去除标题与说明，内容变短后不再需要之前的底部滚动偏移；人像、成长／荣誉和单一退出按钮完整可读。教师／管理员、独立退出确认、登录／画廊及未被当前测试引用的历史图不变。

对比图和像素 bbox 记录位于 `.local-dev/r1-polish-visual-review/`，全部变化已走查。未改变字体、阈值或遮罩；基线及本回执独立提交。设计、验收指南、矩阵与选择器契约随产品提交保存。

最终 Git／Draft／同提交 CI 回执为 `.local-dev/logs/r1-polish-git-ci.json`。保持 Draft，不修改 main 或合并；完整远端 CI 与独立评审不能借用历史 head 的结果。演示服务恢复后沿用三个已有账号再次核对，结果记在 `r1-polish-restored-demo.log`。
