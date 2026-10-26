# AuditLog 规约-测试对账矩阵

> 建立日期：2026-10-25 · 域：AuditLog（§30 字段契约 / 必录动作枚举 / 不可删除 / 快照脱敏 + 管理台只读检索面；测试覆盖活动 test coverage campaign 任务 C）
> 覆盖章节：§30
> 方法论：`campusquest-test-process` skill 规约对账节；**预期列先于测试检索从 spec 原文落笔**；缺口列出不补；不匹配上报不擅改。
> 规约来源：spec §30（动作枚举含 owner 2026-10-11 扩列与 V1.1 2026-10-15/2026-10-16 增补）。
> 域界（已覆盖章节不重复建行，让渡如下）：Task 生命周期 publish/pause/resume/close/archive（含 before/after status 快照）、协作者授予/移除、Assignment 批量导入确认 → `tasks-square.md` S9；评论管理删除 → `community.md` M8；匿名身份追溯（揭示审计）→ `identity-rbac.md` I13 与 `community.md` M11；User suspend/ban/unban → `identity-rbac.md` I12（状态机转移同事务审计）+ `admin-powers.md` AP5（批量逐行审计）；记住设备清退 OWN_DEVICES_REVOKED → `identity-rbac.md` I20；注册审批 USER_APPROVED/USER_REJECTED → `registration-approval.md` RA6；平台注册表 PLATFORM_CREATED/PLATFORM_UPDATED → `platform-registry.md` PR6/PR7。登录流行为本身在 `unified-login.md`（其矩阵未含审计行断言，§30 的登录审计行落点仍在本表，见 G-3/G-4）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 11 | AL1-AL5、AL8、AL9、AL12-AL15 |
| 缺口（列出不补） | 4 | G-1（AL6 Task 级状态修复）、G-2（AL7 StudentWhitelist 变更审计——面已退役）、G-3（AL10 邮箱码登录审计行）、G-4（AL11 手机码登录审计行） |
| 不匹配 | 0 | — |
| 流程行 | 0 | — |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| AL1 | §30 字段清单 | AuditLog 行至少携带 id/actor_user_id/action/资源类型/资源 id/before_snapshot/after_snapshot/reason/ip_address/request_id/created_at；写者 flush-only 入调用方事务 | `tests/integration/audit/test_audit_log.py::test_writer_appends_one_row_committed_by_the_caller`（id/created_at 库生成、actor/action/target/reason/details 落行、调用方 commit 持久化）+ `tests/integration/audit/test_audit_log.py::test_writer_persists_the_s30_snapshot_and_request_columns`（before/after 快照与 ip/request_id 逐字落行；非 HTTP 调用四列 NULL 为文档化默认） | 已覆盖 |
| AL2 | §30「Admin 积分调整」 | Admin 积分调整写一行审计（同事务） | `tests/integration/points/test_admin_adjustment.py::test_adjustment_commits_its_audit_row_in_the_same_transaction`（配套：同文件拒绝族证明拒写的调用零审计行——:287-294 audit_rows==0） | 已覆盖 |
| AL3 | §30「奖励冲销」 | 奖励冲销写一行持久审计行（audited before 取锁时点余额） | `tests/integration/points/test_reward_reversal.py::test_reversal_writes_one_durable_audit_row` + `tests/integration/points/test_reward_reversal.py::test_reversal_before_snapshot_reads_the_lock_time_balance`（round-5 P1：并发下 audited before 是 FOR UPDATE 锁定时点的余额） | 已覆盖 |
| AL4 | §30「Submission 通过/退回/恶意锁定失效」 | 三类审核动作各写审计行：通过（状态迁移行）、退回（REVISION_REQUIRED 迁移+note）、恶意锁定失效（锁迁移+脱敏依据）；幂等重放不写第二行；HTTP 动作携带 request_id/ip | `tests/integration/submissions/test_review_audit.py::test_approve_writes_one_audit_row_with_the_state_migration` + `tests/integration/submissions/test_review_audit.py::test_require_revision_audits_the_migration_and_note` + `tests/integration/submissions/test_review_audit.py::test_invalidate_audits_the_lock_migration_and_redacted_basis` + `tests/integration/submissions/test_review_audit.py::test_approve_idempotent_replay_writes_no_second_row` + `tests/integration/submissions/test_review_audit.py::test_http_review_actions_carry_request_id_and_ip` | 已覆盖 |
| AL5 | §30「RewardRedemption 审批、发放」 | 每个生效决定写一行审计（REDEMPTION_APPROVE/_REJECT/_FULFILL，actor=Admin，target=redemption，reject reason 随行）；幂等重放与被拒调用零行 | `tests/integration/points/test_points_api.py::test_redemption_decisions_write_durable_audit_rows`（三动作各一行、approve 重放不加行、teacher 被拒零行） | 已覆盖 |
| AL6 | §30「Task 强制关闭/状态修复」 | Task 级强制关闭与状态修复写审计行 | 拆半：**强制关闭**=Admin 经生命周期 close 动词（backend/app/modules/tasks/service.py close_task，owner/admin 可调），其审计行已由 tasks-square S9 钉死（`tests/integration/tasks/test_lifecycle_audit.py::test_lifecycle_verbs_each_append_one_audited_row`，让渡不重复建行）；**Task 级状态修复**无任何实现——修复服务公面恰两命令且被结构测试钉死：`tests/integration/audit/test_repairs.py::test_service_public_surface_is_exactly_the_two_named_commands`（release_occupied_assignment + force_fail_delivery，无 Task 修复词汇） | 缺口 G-1（仅 Task 级状态修复半边；强制关闭半边让渡 S9） |
| AL7 | §30「StudentWhitelist 重要变更」 | 白名单重要变更写审计行 | 无测试可引：白名单时代已被 V1.1 spec 2026-10-16 §1 开放注册终结（backend/app/modules/identity/service.py:59「the whitelist era ended」；`registration-approval.md` 域界即「白名单退役」）——现存代码无 StudentWhitelist 管理写面、无对应审计动作常量、`tests/` 全域零命中 | 缺口 G-2（spec §30 该行所指面已退役，行本身待修订裁决——按纪律列出不补） |
| AL8 | §30「系统配置变更」 | 系统配置写入审计（每次写 bump version 并入审计行 details.version）；被拒写入零行零审计 | `tests/integration/system/test_system_settings_registry.py::test_each_write_bumps_version_and_audits_it` + `tests/integration/admin/test_admin_api.py::test_audit_log_search_filters_and_paginates`（检索侧证：SYSTEM_SETTING_UPDATED 行 target_id=设置键可过滤） | 已覆盖 |
| AL9 | §30「手工修复 Claim/Assignment 状态」 | 命名修复命令写一行审计：reason、before/after 可用性快照、claim 事实入 details、request_id/ip 同行，且与修复同事务；被拒修复零行 | `tests/integration/audit/test_repairs.py::test_release_returns_dangling_occupied_assignment_to_available`（恰一行+快照对+关联列全断言）+ `tests/integration/audit/test_repairs.py::test_force_fail_flips_sending_to_failed_and_audits`（邻接第二命令：delivery 强制失败同构审计）+ `tests/integration/audit/test_repairs.py::test_release_refuses_non_occupied_assignment_with_a_typed_409`（拒绝零审计行） | 已覆盖 |
| AL10 | §30 邮箱验证码登录扩列（V1.1 2026-10-15） | EMAIL_LOGIN_SUCCEEDED（channel=email_code/+device/+totp 快照）与 EMAIL_LOGIN_TOTP_REQUIRED 各写审计行；actor=登录账号本人；行内不落邮箱（G11） | 无测试可引：服务侧写行存在于 backend/app/modules/identity/email_login_service.py:388/418/452/546（含 channel 快照），但 `tests/` 全域对 EMAIL_LOGIN_SUCCEEDED/EMAIL_LOGIN_TOTP_REQUIRED 零命中；identity-rbac I17 引用的 `test_audit_rows_for_success_and_totp_required` 已随 PR-3 路由族退役，unified-login 矩阵 U 行亦不含审计断言 | 缺口 G-3 |
| AL11 | §30 注册审批扩列末句（V1.1 2026-10-16） | 学生手机码登录成功写 MOBILE_LOGIN_SUCCEEDED 审计行（channel=mobile_code/+device 快照，行内不落手机号 G11） | 无测试可引：服务侧写行存在于 backend/app/modules/identity/mobile_login_service.py:368/729，`tests/` 全域对 MOBILE_LOGIN_SUCCEEDED 零命中 | 缺口 G-4 |
| AL12 | §30 末段第 1 句 | AuditLog 不允许通过普通管理后台删除（不可变历史） | `tests/integration/audit/test_audit_log.py::test_audit_log_model_is_append_only_by_convention`（全列无 onupdate、无 updated_at、唯一 INSERT 写者=AuditLogWriter.append）+ `tests/integration/audit/test_repairs.py::test_no_destructive_repair_method_exists`（禁面清单显式含 delete_audit_log）+ 管理台面仅 GET /admin/audit-logs 与两个修复 POST、无删除端点（backend/app/modules/audit/router.py 全文） | 已覆盖（约定式+结构钉，同 ledger 0007 裁决） |
| AL13 | §30 末段第 2 句 | 敏感 before/after snapshot 必须脱敏：密码 hash、OTP、完整 Refresh Token 等禁入（任意嵌套深度） | `tests/integration/audit/test_audit_log.py::test_writer_redacts_secrets_from_every_payload_column`（password_hash/otp/refresh_token/access_token/totp_secret/recovery_codes 任意嵌套→[REDACTED]，reason 为自由文本豁免）+ 单元族 `tests/unit/audit/test_redaction.py::test_redacts_every_secret_at_any_nesting_depth` / `tests/unit/audit/test_redaction.py::test_covers_the_full_secret_vocabulary` / `tests/unit/audit/test_redaction.py::test_matches_key_names_case_insensitively` | 已覆盖 |
| AL14 | §30 字段 ip_address/request_id（关联穿线） | HTTP 审计动作从请求穿线 request_id 与客户端 ip；无 header 时仍可关联；敌意 header 不透传 | `tests/integration/submissions/test_review_audit.py::test_http_review_actions_carry_request_id_and_ip` + `tests/integration/submissions/test_review_audit.py::test_http_without_request_id_header_still_correlates` + `tests/unit/audit/test_audit_context.py::test_resolved_request_id_is_kept_verbatim` + `tests/unit/audit/test_audit_context.py::test_hostile_header_resolves_to_the_replacement_not_the_header` | 已覆盖 |
| AL15 | §30（管理后台访问面的只读半边；G10 门） | 管理台审计检索：分页（上限 50）新到旧、action/actor/target 等值过滤、半开时间区间；DTO 逐字回放落库行（写侧脱敏是不变量，读侧无二次脱敏）；非 Admin 403 | `tests/integration/admin/test_admin_api.py::test_audit_log_search_filters_and_paginates`（过滤/分页/序；未过滤页 total 下限）+ `tests/integration/admin/test_admin_api.py::test_teacher_direct_call_is_permission_denied`（守门） | 已覆盖 |

## 缺口明细（列出不补）

- **G-1**（AL6）：Task 级状态修复命令不存在——`RepairService` 公面被 `test_service_public_surface_is_exactly_the_two_named_commands` 结构测试钉死为恰两命令（Assignment 占用释放、Delivery 强制失败），无 Task 状态修复词汇、无路由、无测试；§30 该行的「强制关闭」半边已由生命周期 close 动词（Admin 可调）+ tasks-square S9 审计覆盖（让渡）。
- **G-2**（AL7）：StudentWhitelist 重要变更审计——白名单管理面已随 V1.1 2026-10-16 §1 开放注册整体退役（identity/service.py:59；注册改走 PENDING_APPROVAL+审批，审批审计在 registration-approval RA6），现存无写面即无审计对象与测试；spec §30 该行走留待修订裁决（过时行 vs 需求遗漏），按纪律列出不补。
- **G-3**（AL10）：EMAIL_LOGIN_SUCCEEDED / EMAIL_LOGIN_TOTP_REQUIRED 审计行零测试——服务侧确实写行（email_login_service.py 四处，含 channel 快照与「行内不落邮箱」形状），但 identity/unified-login 两矩阵与 `tests/` 全域均无断言（I17 引用的旧断言件已随 PR-3 退役）；含 actor=本人与 G11 邮箱不落行两个子断言面。
- **G-4**（AL11）：MOBILE_LOGIN_SUCCEEDED 审计行零测试——mobile_login_service.py:368/729 写行（channel 快照），`tests/` 全域零命中；G11 手机号不落行子断言面同缺。

## 不匹配明细

无。本域未发现测试行为与 spec 推导预期冲突的条目。

## 备注（实现取舍与结构注记，非缺口）

- 字段命名映射：spec 的 resource_type/resource_id 落地为多态对 `target_type`/`target_id`（语义等价，ledger source_* 同判）；实现额外携带 `actor_role`（动作时角色快照，VARCHAR 不 CHECK——冻结历史不随角色词汇演进破裂）与 `details`（JSONB 结构化上下文）。`actor_user_id` 刻意无外键：审计行存续超越所指账号的删除（models.py docstring 裁决）。
- AL12 的不可删是**约定式强制**（模型无 UPDATE/DELETE 路径 + 唯一 INSERT 写者 + 修复服务禁面钉 + 审查），DB 不加触发器——与 points_ledger 的 0007 裁决同判，测试 docstring 记录该 ruling。
- 登录类审计动作（G-3/G-4）的写入代码在 PR-3 路由退役中存活（服务层被 unified challenge 复用），故缺口是「断言缺失」而非「行为缺失」——补断言时应同时钉 channel 快照与行内不落邮箱/手机号（G11）两个面。
- 让渡汇总见文件头「域界」行；被让渡章节的对账以所引矩阵行为准，本表不重复建行。
