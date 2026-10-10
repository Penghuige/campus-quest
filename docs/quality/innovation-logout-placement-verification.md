# R1 体验调整：退出入口移到“我的”最底端

2026-10-10，起点 `747ced3d06172a70bcf8a8e7627f520397c0b4e7`，沿用 `codex/innovation-platform`。用户明确指定位置，属于已确认 R1 的局部体验调整；未实施 R2 或修改后端状态、接口、迁移。下列证据为本次新跑，不用 R1 历史测试替代。

## 行为与验收

学生顶栏移除退出链接；“我的”的两个分区都在当前内容之后提供单一“退出当前账号”区域。使用已有 Button、样式与独立 `/logout` 确认流程；入口随页面滚动。教师/管理员工作台原退出入口保留。验收：打开 `/profile`，下滑到底点击“退出登录”，再确认；随后在“个人信息”分区重复。应返回登录页，旧私密表单不可恢复。

本机已有演示账号的位置探针先在原实现失败：默认“我的档案”没有退出区域（`r1-footer-red.log`，出口 1）。修改后三个真实预置账号的默认／个人信息分区均验证退出位于成长内容／密码设置之后，页面只有一个退出链接，顶栏没有退出；真实注销响应 204（`r1-footer-layout-green.log`，出口 0）。探针不修改内容或授权。

## 本次门禁

完整日志位于仓库外 `C:/Users/111/Desktop/华师令/.local-dev/logs/`。Windows 与 Linux 均使用 Node 22.23.3；浏览器使用独占 `campusquest_test_r1_20261010`，真实 PostgreSQL、Redis、HTTPS MinIO、ClamAV；world 只准备和清理自身对象，未重置开发库或共享测试库。

| 验证 | 完整日志 | 本次结果 |
| --- | --- | --- |
| 类型、lint、CSS、单元、覆盖与构建 | `r1-footer-node22-{typecheck,lint,check-css,test-unit,coverage-ratchet,build}.log` | 全部出口 0；640 单测通过、0 跳过；覆盖 95.47/91.56/90.43%，原下限 95.21/91.22/90.19% 不变 |
| 最终视觉测试修改后的类型与 lint | `r1-footer-final-typecheck.log`、`r1-footer-final-lint.log` | 出口 0；只补视觉测试的实际底部视口断言，产品树未再次修改 |
| 完整浏览器与豁免检查 | `r1-footer-battery.log`、`r1-footer-battery-report.json`、`r1-footer-no-skips.log` | 113 通过、29 既有明文豁免、0 失败/flake，10.5 分钟；含 8 项 Firefox/WebKit 窄 smoke；全部 spec 的 no-skip 出口 0 |
| 依赖审计 | `r1-footer-audit.log` | 出口 0；5 项既有开发链豁免不变，无未豁免 high/critical |
| Linux 固定字体像素 | `r1-footer-visual-regression-final.log`、`r1-footer-innovation-review-visual-final.log`、`r1-footer-bottom-visual-final.log` | 原 16 + 审核 3 + 导航 4 = 23 PNG 最终无更新比较通过；审核／导航场景 axe 0；390px 下滑到底后退出按钮底边不超过底栏上边 |
| 规约与选择器 | `backend/scripts/check_test_matrix.py`，矩阵 IN10、选择器契约 R1 段 | 14 份矩阵通过；修改现有测试与探针，未新增 skip 或改变豁免 |
| 待交付源与 Linux 测试源 | `r1-tree-verify.py` | 323 个前端文件匹配；文本归一 LF，PNG 精确；只排除两份独占测试库 harness 配置 |
| 恢复后的本机服务 | `r1-footer-restored-demo.log` | API 8200／Web 3000 恢复；三个已有账号登录、导航、两分区底部位置及实际 logout 204 通过，出口 0；未修改内容或授权 |

第一次误用系统 Node 24.19.0 时，覆盖收集器输出格式与仓库解析器不兼容，`r1-footer-coverage-ratchet.log` 出口 1；未改解析器或降低地板。改回项目／CI 的 Node 22.23.3 后重新执行完整静态、单位、覆盖与构建并通过。该失败日志保留，不算产品测试通过证据。

视觉测试最终增加实际滚动到底的视口检查，避免 fullPage 截图在重排固定底栏时造成遮挡假象；修改后重新首建该新增基线，再无更新比较。原 16／审核 3 场景的产品源和测试源没有随后修改。会话 fence 专项和子路径构建本次没有重跑；它们未改动，历史结果只见 R1 原验证记录。

## 有意基线变更走查

基于 `747ced3` 逐图生成新旧对比并检查全部 33 张历史 PNG。14 张桌面变化只在学生账号区域 `(1221, 12)–(1415, 64)`；尺寸及正文像素完全一致：student-owner-profile-form、student-project-draft-form、student-achievement-draft-form、student-dashboard、student-tasks、student-task-detail、student-claim、student-rankings、student-rewards、student-notifications、achievement-owner-proof-workspace、achievement-operator-empty-queue、achievement-campus-empty-list、innovation-campus-landing。

第 15 张 `innovation-mobile-full-menu` 移除退出后顶栏由两行收为一行，背景正文随顶栏上移；菜单内容、顺序、居中布局及手机五栏不变。新增 `innovation-profile-bottom-logout` 为 390×844 的实际底部视口：退出区域在成长／荣誉内容之后，按钮完整位于底栏上方。其余 18 张历史 PNG（教师／管理员、登录／画廊、独立退出确认及未被当前测试引用的历史图）字节不变。对比图及 bbox 报告在 `.local-dev/r1-footer-visual-review/`；未放宽阈值、遮罩或字体契约。基线独立提交。

## 保存与远端检查

验收指南 `docs/demo/innovation-navigation.md`、设计补充、矩阵 IN10、选择器契约及 CI 的导航截图数量同步更新。实际最新 Git／Draft／同提交 CI 状态由 `.local-dev/logs/r1-footer-git-ci.json` 保存；完整远端 CI 必须对准本次最终 head，GitGuardian 单项不能代表完整 CI。既有 R1 独立评审不能冒充本次 head 的新 APPROVE；本次小范围调整未另开代理评审，也没有合并。
