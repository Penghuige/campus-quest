# 创新创业项目草稿前端规约对账

2026-10-09补充：`innovation-drafts.spec.ts` 三项自动浏览器用例已在真实隔离测试世界全部执行通过，含390×844键盘保存、跨页面冲突和创建响应丢失重试；覆盖下表F4/F5/F6中原先“尚未执行”的部分。F7的Linux像素基线仍待补齐。两处alert定位已收窄至命名草稿编辑器，保留原断言。

依据：`docs/superpowers/plans/2026-10-08-innovation-project-drafts.md`；以下预期在编写测试和实现前记录。后端权限、PostgreSQL 并发与幂等由创新域后端矩阵单独核对。

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| F1 | 五字段私人准备区 | 仅名称必填，五字段 trim，未完成的其他字段可保存 | `tests/../../frontend/src/__tests__/innovation-drafts.test.ts` incomplete private overview | 已覆盖（单测通过） |
| F2 | 字符上限 | 按 Unicode code point 计数，五字段边界内可保存，超限就地提示 | `tests/../../frontend/src/__tests__/innovation-drafts.test.ts` field limits count Unicode code points | 已覆盖（单测通过） |
| F3 | 独立本人接口 | 列表分页、详情、新建、PATCH 使用 IE 本人路径；创建携带 UUID request_id，修改携带 version，不发送 owner/发布字段 | `tests/../../frontend/src/__tests__/innovation-drafts.test.ts` IE requests use private paths | 已覆盖（单测通过） |
| F4 | 失败状态 | 保存失败不得报告成功；版本冲突明确提示并保留输入，网络错误可重试 | `tests/../../frontend/src/__tests__/innovation-drafts.test.ts` save errors；`tests/../../frontend/e2e/innovation-drafts.spec.ts` stale save / lost create response | 已覆盖（错误映射单测通过，浏览器冲突与重试用例尚未执行） |
| F5 | 可用闭环 | 个人中心进入，创建，刷新后仍能打开、修改、再次保存真实草稿 | `tests/../../frontend/e2e/innovation-drafts.spec.ts` create, reload, edit and keyboard-save | 已覆盖（root 真实浏览器创建/刷新/修改通过；自动 e2e 尚未执行） |
| F6 | 第一类手机和键盘状态 | 五字段有可访问名称，Tab/键盘可保存；手机无横向溢出；内部 UUID 不作为正文展示 | `tests/../../frontend/e2e/innovation-drafts.spec.ts` create, reload, edit and keyboard-save | 已覆盖（root 390×844 无横向溢出；实际 Tab 从名称进入简介且有可见焦点，Enter 空表单聚焦名称并报错；完整自动键盘 e2e 尚未执行） |
| F7 | 新稳定屏 | 固定 sans/mono 字体捕获新建表单，不改旧基线；Linux 权威 PNG 必须另行真实生成和检查 | `tests/../../frontend/e2e/visual-regression.spec.ts` student-project-draft-form；G-1 | 缺口 G-1 |
| F8 | 本人私有资料隔离 | 登录账号改变后，已显示的前一账号草稿必须清除；旧会话迟到响应不得进入新界面 | `tests/../../frontend/src/__tests__/innovation-drafts.test.ts` a successful private response from an old auth context is discarded | 已覆盖（单测通过；root 在A标签已打开草稿时于另一标签登录B，原标签自动切换B并显示空列表；恢复A后原草稿仍在） |

测试路径相对于 backend 根目录，沿用现有 matrix checker 的 `tests/` 路径识别约定。自动 e2e 使用专用可丢弃浏览器世界；本轮未运行会重置数据的现有 runner。

## 缺口明细

- **G-1**：新建表单的固定字体截图用例与 no-skip 登记已添加；Linux Chromium 权威 PNG 尚未真实生成和人工检查。此项是合并前门禁缺口，不以 Windows 实机截图替代。
