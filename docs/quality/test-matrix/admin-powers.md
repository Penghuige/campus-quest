# 管理员权力包与运营便利 规约-测试对账矩阵

> 建立日期：2026-10-18 · 域：V1.1 Wave 2a（spec §3 管理员权力包+§4.1/§4.2 运营便利；§4.3 已作废 547099b）
> 方法论：`campusquest-test-process` skill 规约对账节；**预期列先于测试检索落笔**。
> 契约来源：spec 原文+前后端 2026-10-18 冻结答复（batch reason=稳定错误码；overview 全键集；whitelist 口径取消）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 前端已覆盖（后端半随其 PR） | 7 | AP1-AP7（手写钉+UI+e2e 契约钉就位；后端 wave2a-admin-powers 分支实现中） |
| 缺口（轻，列出不补） | 0 | — |
| 不匹配 | 0 | — |
| 流程行 | 0 | — |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| AP1 | §4.1 仪表盘 | `GET /admin/overview` 一次性聚合；by_role/by_status/tasks.by_status 全固定键集（零值带键不漂移）；whitelist 口径不存在（§4.3 作废）；/admin 首页=仪表盘、users 留导航、登录落点于此 | 前端：`frontend/src/__tests__/admin-powers-api.test.ts`（线缆钉）+ `frontend/e2e/admin-powers.spec.ts`（仪表盘渲染钉）；后端集成行随其 PR | 前端已覆盖；后端待其 PR |
| AP2 | §3.1 用户详情 | `GET /admin/users/{id}` 聚合 profile/状态/角色/钱包余额/最近 10 提交/活跃会话数/记住设备数；users 行用户名点入详情页 | `frontend/e2e/admin-powers.spec.ts`（详情全景断言） | 前端已覆盖；后端待其 PR |
| AP3 | §3.2 代重置密码 | `POST /admin/users/{id}/password-reset` 签发一次性链接（48h）；前端仅显示一次+复制件；不可用于自身（UI 隐藏+服务端拒绝） | `frontend/e2e/admin-powers.spec.ts`（一次性链接显示钉） | 前端已覆盖；后端待其 PR |
| AP4 | §3.3 会话管理 | `GET /sessions` 列表+`DELETE` 撤销全部 refresh+记住设备；被踢用户下一请求 401 | `frontend/e2e/admin-powers.spec.ts`（踢线后 401 探针钉） | 前端已覆盖；后端待其 PR |
| AP5 | §3.4 角色升降 | `PATCH /role` TEACHER↔ADMIN；不可自身（UI 隐藏）、不可降级末位 ACTIVE ADMIN（服务端） | `frontend/e2e/admin-powers.spec.ts`（角色往返钉 TEACHER→ADMIN→TEACHER） | 前端已覆盖；后端待其 PR |
| AP6 | §3.5 批量操作 | `POST /users/batch` {action: suspend\|reactivate, user_ids≤100}；逐行结果 {user_id, ok, reason?=稳定错误码}；前端码→文案映射非自由文本 | `frontend/e2e/admin-powers.spec.ts`（批量停用/恢复往返钉）+`frontend/src/__tests__/admin-powers-api.test.ts`（reason 形态钉） | 前端已覆盖；后端待其 PR |
| AP7 | §4.2 邀请生命周期 | `/admin/staff/invitations` 族：列表四态（PENDING/ACCEPTED/EXPIRED/REVOKED）；DELETE 撤销未接受（token 立即失效）；POST resend 答一次性 {link}（旧 token 失效）；ACCEPTED 行无动作；创建表单保留 | `frontend/e2e/admin-powers.spec.ts`（创建→撤销→重发链钉）+`frontend/src/__tests__/admin-powers-api.test.ts`（线缆钉） | 前端已覆盖；后端待其 PR |
