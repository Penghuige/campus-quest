# 创新创业项目草稿前端规约对账

2026-10-09补充：`innovation-drafts.spec.ts` 三项自动浏览器用例已在真实隔离测试世界全部执行通过，含390×844键盘保存、跨页面冲突和创建响应丢失重试；覆盖下表F4/F5/F6中原先“尚未执行”的部分。F7的Linux像素基线仍待补齐。两处alert定位已收窄至命名草稿编辑器，保留原断言。

依据：`docs/superpowers/plans/2026-10-08-innovation-project-drafts.md`；以下预期在编写测试和实现前记录。后端权限、PostgreSQL 并发与幂等由创新域后端矩阵单独核对。

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| F1 | 五字段私人准备区 | 仅名称必填，五字段 trim，未完成的其他字段可保存 | `tests/../../frontend/src/__tests__/innovation-drafts.test.ts` incomplete private overview | 已覆盖（单测通过） |
| F2 | 字符上限 | 按 Unicode code point 计数，五字段边界内可保存，超限就地提示 | `tests/../../frontend/src/__tests__/innovation-drafts.test.ts` field limits count Unicode code points | 已覆盖（单测通过） |
| F3 | 独立本人接口 | 列表分页、详情、新建、PATCH 使用 IE 本人路径；创建携带 UUID request_id，修改携带 version，不发送 owner/发布字段 | `tests/../../frontend/src/__tests__/innovation-drafts.test.ts` IE requests use private paths | 已覆盖（单测通过） |
| F4 | 失败状态 | 保存失败不得报告成功；版本冲突明确提示并保留输入，网络错误可重试 | `tests/../../frontend/src/__tests__/innovation-drafts.test.ts` save errors / refused draft saves；`tests/../../frontend/e2e/innovation-drafts.spec.ts` stale save / lost create response | 已覆盖（错误映射单测通过；浏览器冲突与重试用例已执行通过） |
| F5 | 可用闭环 | 个人中心进入，创建，刷新后仍能打开、修改、再次保存真实草稿 | `tests/../../frontend/e2e/innovation-drafts.spec.ts` create, reload, edit and keyboard-save | 已覆盖（真实浏览器与自动 e2e 创建/刷新/修改通过） |
| F6 | 第一类手机和键盘状态 | 五字段有可访问名称，Tab/键盘可保存；手机无横向溢出；内部 UUID 不作为正文展示 | `tests/../../frontend/e2e/innovation-drafts.spec.ts` create, reload, edit and keyboard-save | 已覆盖（390×844 手机、键盘自动 e2e 已执行通过） |
| F7 | 新稳定屏 | 固定 sans/mono 字体捕获新建表单，不改旧基线；Linux 权威 PNG 必须另行真实生成和检查 | `tests/../../frontend/e2e/visual-regression.spec.ts` student-project-draft-form | 已覆盖（Linux生成、走查及重复比较通过；完整像素门禁另有既有任务列表内容差） |
| F8 | 本人私有资料隔离 | 登录账号改变后，已显示的前一账号草稿必须清除；旧会话迟到响应不得进入新界面 | `tests/../../frontend/src/__tests__/innovation-drafts.test.ts` a successful private response from an old auth context is discarded | 已覆盖（单测通过；root 在A标签已打开草稿时于另一标签登录B，原标签自动切换B并显示空列表；恢复A后原草稿仍在） |
| F9 | 五字段编辑与创建幂等 | 编辑状态不继承资源元数据；任何字段改变均识别为内容变化，防误复用创建键 | `tests/../../frontend/src/__tests__/innovation-drafts.test.ts` editing a saved project excludes resource metadata and detects every unsaved field | 已覆盖（单测通过；不替代丢失回包浏览器验证） |

测试路径相对于 backend 根目录，沿用现有 matrix checker 的 `tests/` 路径识别约定。自动 e2e 使用专用可丢弃浏览器世界；本轮未运行会重置数据的现有 runner。

## 2026-10-09 覆盖率补验预期

依据项目草稿计划的五字段编辑、稳定创建请求键与错误保留输入约定：载入编辑器只取五个可编辑字段，不复制服务端资源元数据；任意一个字段改变均应识别为未保存变化，不能误复用另一份内容的创建请求。保存错误需区分同键不同内容、停用、不可访问、字段校验与系统失败，且系统失败保留请求追踪号。本轮先记录这些预期，再补单测；不改变业务实现或降低覆盖门槛。

## 缺口明细

- **G-1 已闭合**：Linux 初始 PNG 已生成、走查及重复比较通过；环境、范围与完整门禁剩余限制见 `docs/quality/innovation-linux-visual-walkthrough.md`。
