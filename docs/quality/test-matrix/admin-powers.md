# admin powers 规约-测试对账矩阵

> 建立日期：2026-10-19（前端半 2026-10-18 先行，编号随 #114 后端权威版重排）· 域：V1.1 Wave 2a 管理员权力包+仪表盘+邀请生命周期（spec 2026-10-15-v1.1 §3/§4）
> 方法论：`campusquest-test-process` skill；**预期列先于测试检索落笔**；缺口列出不补；不匹配上报不擅改。
> 规约来源：v1.1-experience-and-administration-design §3.1-§3.5 / §4.1-§4.2；G12（每动作审计行）；G11（邮箱明文只存行）。契约冻结答复 2026-10-18（batch reason=稳定错误码；overview 全键集；whitelist 口径取消——§4.3 作废）。
> 域界：邀请创建/接受五步在 identity-rbac I4（互引）；本表只收 §3/§4 的管理面新行为。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖（前后端齐） | 10 | AP1-AP10 |
| 缺口 | 0 | — |
| 不匹配 | 0 | — |
| 流程行 | 0 | — |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| AP1 | §3.1 | 用户详情全景一次读聚合：profile/状态/角色/钱包余额/最近 10 条提交（经 claim→task 取标题）/活跃会话数/记住设备数；未知 id 404；users 行用户名点入详情页 | 后端：`tests/integration/admin/test_admin_user_detail_and_sessions.py::test_admin_reads_the_user_panorama` / `::test_unknown_user_answers_404_across_the_detail_family`；前端：`frontend/e2e/admin-powers.spec.ts`（详情全景断言+点入链） | 已覆盖 |
| AP2 | §3.3 | 踢线：DELETE /admin/users/{id}/sessions 撤销其全部 refresh token+记住设备凭据（被盗应急）；审计 ADMIN_SESSIONS_REVOKED 同事务；GET 侧只披露两计数；未知 id 404 | 后端：`test_admin_user_detail_and_sessions.py::test_kick_revokes_sessions_and_devices_with_audit` / `::test_unknown_user_answers_404_across_the_detail_family`；前端：`frontend/e2e/admin-powers.spec.ts`（踢线后 401 探针钉） | 已覆盖 |
| AP3 | §3.2 | 代重置：一次性链接（urlsafe32/哈希落库/48h）；不可用于自身；学生 403（走手机码流）；未知 404；审计 ISSUED/CONSUMED；消费=设新密码（band 校验）+revoke_all；未知/过期/非 ACTIVE 持有者 token 统一 OTP_TOKEN_INVALID（无 oracle） | 后端：`tests/integration/admin/test_admin_password_reset_link.py`（五测）；前端：`frontend/e2e/admin-powers.spec.ts`（一次性链接显示钉+self 隐藏 UI 钉） | 已覆盖 |
| AP4 | §3.4 | 角色升降 TEACHER↔ADMIN：不可改自身；学生不参与 403；目标角色窄化 400；同角色 no-op 零审计；未知 404；不可降级最后一个 ACTIVE ADMIN（服务层计数+0031 触发器 DB 兜底双保险，并发双降恰一成立）；审计 ROLE_CHANGED before/after | 后端：`tests/integration/admin/test_admin_role_change.py`（五测，含独占世界 TRUNCATE 探针）；前端：`frontend/e2e/admin-powers.spec.ts`（角色往返钉 TEACHER→ADMIN→TEACHER） | 已覆盖 |
| AP5 | §3.5 | 批量：单动作 suspend\|reactivate ≤100/批；复用既有状态机语义零新分支；逐行 {ok, reason} 稳定码（NOT_FOUND/CONFLICT/PERMISSION_DENIED-actor 自身行）；失败行不殃及邻行；每行独立审计；前端码→文案映射非自由文本 | 后端：`tests/integration/admin/test_admin_batch_and_overview.py::test_batch_suspends_each_row_with_per_row_results` / `::test_batch_refuses_the_actor_row_and_invalid_transitions` / `::test_batch_caps_at_one_hundred_ids`；前端：`frontend/e2e/admin-powers.spec.ts`（批量停用/恢复往返钉）+`frontend/src/__tests__/admin-powers-api.test.ts`（reason 形态钉） | 已覆盖 |
| AP6 | §4.1 | 概览一次读：按角色/状态用户计数（固定键集零值也带）+任务状态分布+待审提交/兑换队列+today 新增/活跃（业务时区日界；活跃=distinct 活会话用户）；whitelist 口径不存在；/admin 首页=仪表盘、users 留导航、ADMIN 登录落点于此 | 后端：`test_admin_batch_and_overview.py::test_overview_answers_fixed_key_aggregates`；前端：`frontend/src/__tests__/admin-powers-api.test.ts`（线缆钉）+`frontend/e2e/admin-powers.spec.ts`（仪表盘渲染钉） | 已覆盖 |
| AP7 | §4.2 | 邀请列表：邮箱掩码显示（首字符+域，明文不出服务层=G11）；状态四态派生（ACCEPTED/REVOKED 落库时间戳，PENDING/EXPIRED 业务钟读时切分，优先级 accepted>revoked>expired）；签发人 id+nickname | 后端：`tests/integration/admin/test_admin_invitations_lifecycle.py::test_list_derives_the_four_states_with_masked_email`；前端：`frontend/e2e/admin-powers.spec.ts`（生命周期面板四态链）+`frontend/src/__tests__/admin-powers-api.test.ts`（线缆钉） | 已覆盖 |
| AP8 | §4.2 | 撤销：DELETE 对未接受的设 revoked_at（0032，状态活过过期窗）；token 立即失效（accept 统一拒绝谓词含 revoked）；ACCEPTED/已撤销 409；未知 404；服务级非 ADMIN actor 403（纵深）；审计 STAFF_INVITATION_REVOKED | 后端：`test_admin_invitations_lifecycle.py::test_revoke_kills_the_token_and_refuses_terminal_states` / `::test_lifecycle_guards_refuse_non_admin_actors_and_unknown_ids`；前端：`frontend/e2e/admin-powers.spec.ts`（创建→撤销→重发链钉） | 已覆盖 |
| AP9 | §4.2 | 重签：POST resend 对未接受（pending/EXPIRED 救援路径）/已撤销的换发新 token——旧 hash 被替换即旧 token 死、窗口重开、revoked_at 清零复活；新 token 一次性返回（create 契约）；ACCEPTED 409；未知 404；服务级非 ADMIN actor 403 | 后端：`test_admin_invitations_lifecycle.py::test_resend_rotates_the_one_time_token` / `::test_lifecycle_guards_refuse_non_admin_actors_and_unknown_ids`；前端：`frontend/e2e/admin-powers.spec.ts`（重发一次性 {link} 显示钉） | 已覆盖 |
| AP10 | §4.2 | create 占用预检：邮箱已有账号 → 409 EMAIL_ALREADY_BOUND（前端 2026-10-14 实证缺口）；规范化先行（大小写变体命中）；预检拒后零行落库 | 后端：`tests/integration/admin/test_admin_invitations_lifecycle.py::test_create_refuses_an_email_already_held_by_an_account`；前端：创建表单沿用（identity 面），无新钉 | 已覆盖 |

## 缺口明细

无。

## 不匹配明细

无。
