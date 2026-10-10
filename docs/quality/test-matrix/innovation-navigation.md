# R1 导航与退出：规约—测试对账

预期于 2026-10-10 从 R1 design 与 roadmap 推导，先于测试定位。

| ID | 来源 | 预期 | 测试证据 | 状态 |
| --- | --- | --- | --- | --- |
| IN01 | design 范围 | 公共浏览入口与本人管理分开；三库范围真实说明 | `tests/../../frontend/e2e/innovation-navigation.spec.ts` | 已覆盖（desktop public navigation） |
| IN02 | design 导航 | 手机五栏含双创，奖励等全部可达；路径不重叠 current | `tests/../../frontend/e2e/innovation-navigation.spec.ts` + `tests/../../frontend/src/__tests__/navigation-match.test.ts` | 已覆盖（five mobile destinations；段边界和运营 exclude） |
| IN03 | roadmap R1 / RBAC | 只有获授权 ACTIVE STUDENT 有运营菜单；越权直链拒绝 | `tests/../../frontend/e2e/innovation-navigation.spec.ts` + `tests/../../frontend/e2e/innovation-operations.spec.ts` | 已覆盖（真实 403、授权/撤回、账号切换） |
| IN04 | roadmap §4 | ACTIVE 教师/管理员可从工作台进入校内浏览 | `tests/../../frontend/e2e/innovation-navigation.spec.ts` | 已覆盖（staff workspaces；ADMIN 返回 /admin/users） |
| IN05 | spec §5.6/33 | 真实退出后 cookie 失效，刷新受限页需登录 | `tests/../../frontend/e2e/innovation-navigation.spec.ts` + `tests/../../frontend/e2e/cross-browser-smoke.spec.ts` | 已覆盖（real logout；Firefox/WebKit 窄 smoke） |
| IN06 | privacy §40 | 换账号、跨标签、后退不显示旧私密草稿 | `tests/../../frontend/e2e/innovation-navigation.spec.ts` + `tests/../../frontend/e2e/auth-cross-tab-fence.spec.ts` | 已覆盖（real logout removes private forms；显式 login fence 专项） |
| IN07 | G4 | 退出 5xx/网络失败不得假成功，可安全重试；refresh drain 不被超时绕过；应用前缀保留 | `tests/../../frontend/e2e/innovation-navigation.spec.ts` + `tests/../../frontend/src/__tests__/auth-transition-serialization.test.ts` + `tests/../../frontend/src/__tests__/app-path.test.ts` | 已覆盖（503、真实注销丢 ACK、15s 无响应、单位 RED→GREEN；/campus 构建导航探针） |
| IN08 | historical focus debt | 审核完成/失败后焦点到稳定结果/重试/队列；cancel 回原按钮 | `tests/../../frontend/e2e/innovation-review.spec.ts` + `tests/../../frontend/src/__tests__/review-focus.test.ts` | 已覆盖（负责人/运营原请求重试焦点 RED→GREEN，防旧 epoch 回焦） |
| IN09 | quality visual/a11y | 三屏宽可用、长内容不溢出、axe、Linux 像素 | `tests/../../frontend/e2e/innovation-navigation.spec.ts` + `tests/../../frontend/e2e/innovation-navigation-visual.spec.ts` + `tests/../../frontend/e2e/visual-regression.spec.ts` | 已覆盖（320/390/800；退出位置调整后 23 PNG 不更新比较；新页面 axe 0） |
| IN10 | 2026-10-10 用户体验调整 | 学生退出在“我的”两分区内容最底部；顶栏无退出；滚动可达且真实注销 | `tests/../../frontend/e2e/innovation-navigation.spec.ts` + `tests/../../frontend/e2e/innovation-navigation-visual.spec.ts` | 已覆盖（已有本机探针 RED→GREEN；113 浏览器通过；实际手机底部无导航遮挡；详见 innovation-logout-placement-verification.md） |

ID 从初版 R1-01 改为 IN01，以符合既有对账检查器的行识别规则；不修改检查器。文件引用证明存在，具体运行结果见 `docs/quality/innovation-navigation-verification.md`。

## 缺口明细

R1 无待实施功能缺口；远端完整 CI 必须在最终提交单独核验，不能以本地门禁代替。

## 不匹配明细

无 R1 不匹配。已公开成果免复审是已知 R2 差异，保持在总规划和历史矩阵中，本轮不将它标成满足新决定。
