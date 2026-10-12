# R4 人才库 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 学生独立维护人才档案及可选作品文件，经本人确认公开，校内用户可查找和授权联系，关闭/撤回对旧直链同样生效。

**Architecture:** 沿用创新创业模块的服务事务、校内身份和私有响应中间件，独立建立人才草稿/不可变公开版本/命令回执/文件记录。复用存储、扫描和限流端口，增加真实人才文件命名空间；前端表单、API、恢复状态与页面组合分离。

**Tech Stack:** 仓库已安装版本的 FastAPI/Pydantic、SQLAlchemy async、PostgreSQL/Alembic、Redis、MinIO/S3、ClamAV、Next.js/React/TypeScript、pytest、Playwright；不增加依赖。

**Spec:** [2026-10-12-innovation-talent-design.md](../specs/2026-10-12-innovation-talent-design.md)。2026-10-12 用户授权“你站在用户视角感觉合理的话就推进吧”；书面规格由代理再次核对后认可，本计划自行审阅并连续执行，沿用本会话原生执行、最终一次独立评审，不重复请求业务确认。

## Global Constraints

- 当前 feature 分支 codex/innovation-platform，不改 main、不自动合并，PR 保持 Draft；不重置开发数据。
- 本人维护仅 ACTIVE STUDENT，不要求负责人资格；校内浏览沿用当前 ACTIVE 演示身份，学校 SSO 最后。
- 成果更新仍全部复审，旧通过版继续展示；独立招募仍免审且受资格和项目权限限制。
- 私有保存与明确公开分离；版本冲突 409，隐藏/无权公开读取中性 404；所有 ie 响应 private/no-store。
- PDF/PNG/JPEG，非空且每个 ≤10 MiB；最多 10 个活跃库存、公开最多 5 个；15 分钟上传凭据；150 秒检查租约、140 秒等待上限。
- 字段上限及标签/链接/渠道数量逐项使用 spec §3；联系/经历/证明选填，首次可无负责人资格、无作品开启。
- request_id/指纹/预期版本保障幂等；Auth epoch 和条件清理保护跨账号隐私与迟到新输入；不持久缓存表单/联系/File。
- 业务 Clock、真实 PG 锁/约束、真实 storage/scanner 组合、审计 fail-closed；不复用成果附件业务权限。
- 不做 R5+、积分/推荐/SSO、站内申请/私聊；演示资料明确模拟。正式存储保留策略归 R10。

## Review Focus

1. 没有成果、比赛或联系方式的新生：最小填法仍可开启，不误套招募联系或负责人门槛。Task 1 最小资料发布、Task 4 无经验 E2E。
2. 公共档案已有 5 个文件，全部需要替换：先上传另外 5 个再发布，旧版在此之前保留；Task 2 库存/替换 PG 用例。
3. 关闭发生在下载外部 I/O 期间：最终授权再次拒绝，后台扫描/下载不能复活权限；Task 2/3 屏障竞态。
4. 已点发布/保存但没收到回执，随后关闭或继续输入：重试不复活公开、不删新输入；Task 1 命令重放、Task 4 延迟响应测试。
5. Unicode、百分号/下划线、超长外链/文件名和标签：查询按字面、字数与格式两端一致，文件名不变成响应头注入；Task 1/2/3/4 边界用例。

## 文件责任与固定接口

- `talent_models.py`：TalentProfile、TalentRevision、TalentActionReceipt、TalentFile、TalentTag、TalentTagSuggestion；一人一档、公开快照归属、文件状态/大小、回执唯一键由 PG 约束。
- `talent_schemas.py`：TalentFields、TalentLink、TalentContact、TalentSaveCommand、TalentVersionCommand、TalentRevokeContactCommand、TalentFileCommand、TalentProfileResponse、TalentActionResult、TalentTagResponse、TalentSuggestionCommand、PublicTalentSummary/Response/ListResponse、TalentContactsResponse、TalentQuery。
- `talent_service.py`：本人读取/字段保存/明确公开/关闭/撤回、标准词表/建议；`talent_common.py` 只承载事务、fingerprint、身份锁与错误帮助，不依赖招募生命周期。
- `talent_file_service.py`：申请/检查/移除/本人读取；`talent_file_schemas.py`：TalentFileIntentCreate/Response、TalentFileResponse/ListResponse。
- `public_talent_service.py`：公共白名单查询、联系与文件最终授权；两个本人路由和公共路由只做 transport、组合与限流。
- 前端 `talentApi.ts`、`talentForm.ts`、`talentRecovery.ts`、`TalentProfileView.tsx`、`TalentProfileEditor.tsx`、`TalentFiles.tsx`、`TalentContacts.tsx`、`TalentPreview.tsx`、`PublicTalentsView.tsx`、`talentQuery.ts` 分别拥有 API、纯转换、会话恢复、管理组合、表单、文件、联系、预览、公共浏览、URL 筛选。

固定 API（全部 `/api/v1` 前缀）：

| 方法/路径 | 返回与输入 |
| --- | --- |
| GET /ie/me/talent-profile | `{profile: TalentProfileResponse | null}`，未建档为 200/null |
| PUT /ie/me/talent-profile | TalentSaveCommand(request_id, version≥0, fields)，返回 TalentActionResult；version=0 仅首次创建 |
| POST /ie/me/talent-profile/publish、/hide | TalentVersionCommand(request_id, version≥1)，返回 TalentActionResult |
| POST /ie/me/talent-profile/revoke-contact | TalentRevokeContactCommand(request_id, version, channel)，返回 TalentActionResult |
| GET /ie/talent-tags | `{items: TalentTagResponse[]}`；有效标准标签，固定排序 |
| POST /ie/me/talent-tag-suggestions | TalentSuggestionCommand(request_id, name, reason)，安全回执，未处理相同名称去重 |
| GET/POST /ie/me/talent-profile/files | list 返回 TalentFileListResponse；create 返回 TalentFileIntentResponse（201 新建/200 重放） |
| POST /ie/me/talent-profile/files/{file_id}/complete | 完成检查返回 TalentFileResponse，不自动公开 |
| POST /ie/me/talent-profile/files/{file_id}/remove、/revoke | TalentFileCommand(request_id, version)，移除或撤权，返回 TalentActionResult |
| GET /ie/me/talent-profile/files/{file_id}/content | 仅本人 READY/未移除文件，attachment/no-store/nosniff |
| GET /ie/talents、/ie/talents/{id} | PublicTalentListResponse / PublicTalentResponse；列表 TalentQuery(q、tag_ids、cooperation_state、limit、offset) |
| POST /ie/talents/{id}/contacts | `{expected_public_version}` → TalentContactsResponse；人才独立 30/分钟限流 |
| GET /ie/talents/{id}/files/{file_id}/content | expected_public_version query；最终授权与审计后 attachment |

## 验证命令纪律

从 backend 使用 `uv run pytest <files> -q`；真实 PG 使用独占隔离测试数据库 `campusquest_test_r4_20261012`，预检查 require_test_database，通过现有本地依赖访问，绝不清空开发库。Linux 运行环境使用已验证 CampusQuest-Dev 的 docker，而非假定 Windows PATH 有 docker。

从 frontend 使用 `npm.cmd run test:unit`、`npm.cmd run typecheck`、`npm.cmd run lint`、`npm.cmd run check:css`；Node 使用项目已配置的本地 Node 22。门禁完整输出落工作区日志，保留退出码；阅读完整文件而非截断输出替代证据。

每次 API 变化执行 `uv run python scripts/dump_openapi.py ../frontend/openapi.snapshot.json`，随后 frontend `npm.cmd run api:types:snapshot`；契约涉及双树，结束跑双树门禁。

### Task 1: 人才档案与公开版本的真实生命周期

**Files:** Create `backend/app/modules/innovation/talent_models.py`、`talent_schemas.py`、`talent_common.py`、`talent_service.py`、`talent_router.py`、`backend/alembic/versions/0035_innovation_talents.py`；Modify `backend/app/db/base.py`、`backend/app/main.py`；Create `backend/tests/integration/innovation/talent_support.py`、`test_talent_lifecycle.py`、`test_talent_races.py`、`backend/tests/unit/innovation/test_talent_schemas.py`、`docs/quality/test-matrix/innovation-talent.md`。

**Interfaces:**

- TalentFields uses spec §3 names: name/faculty/grade/introduction/advantages/tag_ids/skill_notes/cooperation_direction/desired_roles/weekly_hours/work_mode/cooperation_state/experience/links/file_ids/contacts。输出 fields 私有，公开快照 content 排除 contacts；联系方式只在受限快照专属字段存储，不含公开 DTO。
- `TalentService(clock: Clock, audit: AuditLogWriter | None=None)`；`get_owned(db, *, actor)` 返回上述 nullable 包装；`save(db, *, actor, payload: TalentSaveCommand, context: AuditContext)`；`publish/hide/revoke_contact` 接收各自 typed command/context，返回 `TalentActionResult(action, replayed, applied_version, current: TalentProfileResponse)`。
- `lock_owned(db, *, actor, viewer: Actor | None=None) -> TalentProfile`、`response(db, row) -> TalentProfileResponse`、`replace_public(db, row, *, fields: TalentFields) -> None` 供 Task 2/3 使用。身份排序锁、聚合版本、公开版本与快照全在同一服务事务；文件引用归属/READY/撤回重新锁查。
- TalentProfileResponse 包含 id/fields/version/visibility/public_version/public_contact_channels/public_file_ids/has_unpublished_changes/updated_at；不把 public_revision_id 传前端。模型提前定义 TalentFile 使发布验证真实引用，无文件扫描占位。

- [ ] Step 1：按 spec 写矩阵预测（R4-A01/02/04/07/09），建立真实普通学生/Clock/客户端 helper，无负责人资格；测试 `test_new_student_can_publish_without_work_or_contact`，assert 未建档 GET=200/profile:null、保存后 HIDDEN、发布后 VISIBLE，无 OwnerQualification。
- [ ] Step 2：运行 `uv run pytest tests/integration/innovation/test_talent_lifecycle.py -q`。Expected: 人才接口缺失而预期 200 得到 404 的业务 RED，日志记录；连接失败不是 RED。
- [ ] Step 3：建立上述模型/确定 Alembic 迁移/DTO/服务/薄路由与真实 main 注册；新增标签固定数据不覆盖存量；不修改旧迁移。以 UUID 公开 ID 和唯一 owner_user_id，公开 revision 复合 FK 保证归属。
- [ ] Step 4：增加 schema 与生命周期 RED→GREEN：单字超长、Unicode、重复/无效标签、links http/https、同 key 改载荷、保存不替换旧版、关闭重放旧发布不复活、暂不接洽仍开、撤回最后联系人仍开、治理限制不被开启清除、审核失败无部分写入。字段断言使用 spec §3 精确边界。
- [ ] Step 5：真实独立连接并发 `test_one_profile_creation_race`、`test_publish_hide_version_race`、`test_revoke_publish_race`、`test_account_disabled_before_publication`；用屏障和 Clock，无 sleep 猜测；断言一次有效提交或身份拒绝、无孤立快照/命令。
- [ ] Step 6：隔离库 upgrade→downgrade 0034→re-upgrade→alembic check；运行 `uv run pytest tests/unit tests/integration/innovation/test_talent_lifecycle.py tests/integration/innovation/test_talent_races.py -q` 和后端 ruff format/check、mypy；Expected: 全退出 0；生成双树契约并验证类型，提交 `feat(innovation): add consent-based talent profiles`。

### Task 2: 人才作品上传、扫描与本人撤回

**Files:** Create `talent_file_schemas.py`、`talent_file_service.py`、`talent_file_router.py`（innovation module）；Modify `talent_service.py`、`main.py`、`backend/app/integrations/object_storage.py`、`object_storage_s3.py`、`backend/tests/fakes/integrations.py`；Create `backend/tests/integration/innovation/test_talent_files.py`、`test_talent_file_races.py`、`backend/tests/unit/innovation/test_talent_file_schemas.py`；Extend storage adapter tests/matrix/contracts。

**Interfaces:**

- `ObjectStorage.create_talent_file_upload_url(*, profile_id: UUID, content_type: str, content_length: int, expires_in: int) -> UploadUrl`；真实 adapter 生成独立 talent/<profile>/<random> key，write-once、长度/MIME pinning；现有 achievement 通道不改语义。
- `TalentFileService(clock, storage: ObjectStorage, scanner: EvidenceScanner, audit=None)`：`create_intent(db, *, actor, payload: TalentFileIntentCreate, context) -> tuple[TalentFileIntentResponse,bool]`、`list_owned`、`complete(db, *, actor, file_id, context)`、`remove/revoke(db, *, actor, file_id, payload: TalentFileCommand, context) -> TalentActionResult`、`read_owned_content(...) -> StoredObject`。
- file intent 输入 request_id/name/content_type/size；公开 file metadata 仅 id/name/content_type/size；private file DTO 另有 state/failure_code/version/removed/revoked；签名 URL 仅 intent 响应，日志禁止值。

- [ ] Step 1：`test_unqualified_student_upload_is_private_until_selected` assert 本人真实申请 201、普通档案发布无文件仍成功、PENDING 选中发布 409、READY 后明确发布才引用；先运行 `uv run pytest tests/integration/innovation/test_talent_files.py -q`。Expected: 文件路由 404 的业务 RED。
- [ ] Step 2：新增独立 storage 端口和两 adapter 实现及生产组合；复用检查 primitive，不引用成果 EvidenceService._owned；有界 I/O 不持锁，token/150s/140s fencing，不使用 presigned GET。
- [ ] Step 3：追加并逐个 RED→GREEN：10MiB/10MiB+1、空字节、伪类型/病毒/服务不可用、过期上传凭据后完成既有对象、412 检查确认、重复 key 不重复占库存、5 公开+5 替换/11th拒绝、公开文件不能普通 remove、撤回本人仍能读/不能再发布、移除无内容、上传凭据不能复活记录。
- [ ] Step 4：`test_late_scan_cannot_resurrect_removed_file`、`test_scan_lease_fences_previous_attempt`、`test_owned_read_rechecks_account_after_storage_io` 用屏障/真实 PG；断言旧 token 被丢弃、身份/撤回阻止最终输出、扫描失败不标 READY。
- [ ] Step 5：header injection 文件名与安全 disposition、审计 failure 回滚/不给内容；测试真实 MinIO write-once/MIME/size 与 ClamAV scanner smoke，检查生产 composition。运行 `uv run pytest tests/unit tests/integration/innovation/test_talent_files.py tests/integration/innovation/test_talent_file_races.py tests/integration/test_evidence_scanner_smoke.py -q`；Expected: 全退出0；ruff/mypy/契约再生成，提交 `feat(innovation): add private checked talent files`。

### Task 3: 校内人才查找、授权联系和文件读取

**Files:** Create `backend/app/modules/innovation/public_talent_service.py`、`public_talent_router.py`、`backend/tests/integration/innovation/test_public_talents.py`、`test_talent_contacts.py`、`test_talent_public_file_races.py`；Modify `talent_schemas.py`、`main.py`、`backend/app/integrations/rate_limit.py`；Extend matrix/contracts。

**Interfaces:**

- `PublicTalentService(clock, storage: ObjectStorage, audit=None)`：`list_visible(db, *, actor, query: TalentQuery) -> PublicTalentListResponse`、`get_visible(db, *, actor, profile_id) -> PublicTalentResponse`、`get_contacts(db, *, actor, profile_id, expected_public_version, context) -> TalentContactsResponse`、`read_file_content(..., file_id, expected_public_version, context) -> StoredObject`。
- `TalentQuery` q≤100、去重 tag_ids≤5 全匹配、单 cooperation_state、limit=20(1–50)、offset≥0；排序公开时间 desc/id asc；两身份 ACTIVE + owner STUDENT + VISIBLE/NORMAL + 当前 snapshot；数据库字段白名单投影，不加载草稿/身份私密数据。
- route 组合 S3 adapter 和独立 Redis 桶 ie:talent-contacts；敏感读取先锁双账号、聚合，外部 I/O 后最终锁查版本/状态/文件及审计，锁顺序与 Task 1/2 一致。

- [ ] Step 1：`test_hidden_talent_is_absent_from_search_and_direct_links`、`test_public_projection_excludes_private_sentinels` assert 隐藏人才0条/404、公开不含 student_no/user_id/object_key/contactvalue/草稿 sentinel；运行 `uv run pytest tests/integration/innovation/test_public_talents.py -q`。Expected: 公共路由缺失业务 RED。
- [ ] Step 2：实现上述投影/DTO/Query/router；R4-A03/04 全匹配与通配符字面用例、分页/稳定次序、暂不接洽公开、主人/查看者禁用与治理都隐藏。
- [ ] Step 3：主动联系值仅授权渠道返回；expected_public_version 旧页409、关闭404、30次限流、第31次429、Redis503、审计失败不给值；更新/撤回最后联系人仍可见。
- [ ] Step 4：`test_close_during_public_file_read_denies_final_output`、`test_revoke_during_public_file_read_denies_final_output`、`test_revision_change_requires_refresh_before_contact_or_download`；真人身份、真实 PG+屏障，无旧 URL 或对象 key 绕过；PRIVATE/no-store 连错误一起断言。
- [ ] Step 5：运行 `uv run pytest tests/unit tests/integration/innovation -q`、ruff/mypy、alembic check、真实 adapter smoke；Expected: 全退出0；生成 API 契约，提交 `feat(innovation): add authorized campus talent browsing`。

### Task 4: 本人档案、作品与公共查找前端闭环

**Files:** Create 前述所有 talent 前端文件；Create `frontend/src/app/(student)/profile/talent-profile/page.tsx`、`frontend/src/app/(campus)/innovation/talents/page.tsx`、`frontend/src/app/(campus)/innovation/talents/[id]/page.tsx`；Modify `frontend/src/app/(student)/profile/page.tsx`、`frontend/src/app/(campus)/innovation/page.tsx`、`InnovationNavigation.tsx`、需要的已有 CSS token 使用文件；Create `frontend/src/__tests__/talent-form.test.ts`、`talent-recovery.test.ts`、`talent-query.test.ts`、`frontend/e2e/innovation-talents.spec.ts`、`innovation-talents-visual.spec.ts`；Modify e2e selector/no-skip 契约、矩阵。

**Interfaces:**

- `talentApi` 使用 generated types、统一 api wrapper、每个请求 auth epoch guard；本人 get/save/publish/hide/revoke/contact/tags/suggest/fileIntent/complete/download 等具名函数，上传 fetch PUT 使用签名原始 headers、原始 File，不手写浏览器 Content-Length。
- `talentForm` 暴露 `emptyTalentForm()`、`fieldsToTalentForm(fields)`、`talentFormToFields(form)`、`validateTalentForm(form, mode: 'save'|'publish')`；`talentQuery` parse/build URL 与公共 page/sort 对齐；Unicode 字数使用码点，包含0小时/未填写区别。
- `talentRecovery` 保存当前 session epoch 的输入与原 request_id/载荷，条件比较清理，仅内存；get/put/discard/reset 函数供 editor；迟到 save/scan 不覆盖新输入。

- [ ] Step 1：先写真实转换/恢复/筛选测试：零经历/零contact publish允许、Unicode精确限制、链接拒绝、标准标签映射、auth epoch清空、迟到回执 compare-delete 不删新输入、URL页码返回；运行 `npm.cmd run test:unit`。Expected: 新行为 RED，记录后再实现纯模块。
- [ ] Step 2：实现 API与本人表单/文件/联系/预览；明确保存/公开/关闭/撤回，show scannerstate 可重试；提交中仍保留后来输入；脏表单不可发布旧版；库存替换说明动作，未建档与未公开状态不等同错误。
- [ ] Step 3：实现标准标签建议、公共列表/详情、URL筛选/回列表、按需联系与鉴权 blob下载，不预加载私密值；本人页面导航与创新创业人才入口；沿用 token/Field/Button，必要 Next.js 行为读本地配套文档。
- [ ] Step 4：建立 E2E `new_student_without_evidence_can_enable`、`selected_ready_file_and_contact_are_visible_only_after_confirmation`、`close_denies_old_profile_contact_and_file_links`、`draft_recovery_does_not_cross_accounts`、`late_save_preserves_newer_edit`、`keyboard_error_preview_return_focus`、`scan_retry_and_replace_five_files`；真实模拟资料与双账号，保留测试世界cleanup隔离，不碰开发记录。
- [ ] Step 5：移动/桌面 loading/empty/error/long-input/permission/scan/conflict 走查；axe、键盘focus、公告不读联系值；为新稳定人才页面建立钉字体基线，旧 baseline 必须保持或对因实际新增导航差异单独走查，不更改容差。
- [ ] Step 6：运行 frontend typecheck/lint/check:css/test:unit/build/coverage:ratchet/audit:gate 与人才 E2E/视觉/no-skip；Expected: 全退出0且真实用例数以当轮日志为准；提交 `feat(innovation): deliver student talent workspace and directory`。

### Task 5: 端到端用户验收、独立评审与交付

**Files:** Create `docs/quality/innovation-talent-verification.md`、`innovation-talent-review.md`、`innovation-talent-visual.md`、`docs/handovers/2026-10-12-innovation-r4.md`；更新 roadmap、计划任务状态与最终规约矩阵，按最终结果更新现有 Draft PR 描述。

**Interfaces:** 消费 Tasks 1–4 实际服务/页面/契约/矩阵；交付功能、明确模拟的体验环境、同一最终提交证据与未通过的远端状态，不虚构上线/学校认证。

- [ ] Step 1：隔离环境全量后端单测/真实 PG 集成/worker/适用 E2E及迁移；Windows/Linux双环境前端八门禁、完整浏览器/no-skip、新旧视觉。所有生产组合与输入树哈希对齐；完整输出日志与退出码，Expected: 全适用本地门禁0。
- [ ] Step 2：按 R4-A01–12 逐行对账，不以套件绿替代需求；亲自从普通学生无经验建档→模拟上传→私有预览→公开→观察者搜索联系下载→主人关闭→旧链接拒绝→本人可维护；检查5文件替换与反馈合理性。记录实际屏幕阅读器未执行边界，不用axe替代读屏。
- [ ] Step 3：生成本计划 review-package，最终一次独立全分支评审；开始评审前向用户说明模型/强度。Critical/Important 作者一次修复，RED→GREEN+完整相关套件，不派第二次复审；Minor与任何Ruling按成本列交付。
- [ ] Step 4：恢复演示 Web/API/Worker/Beat，保留全部开发数据、关闭本次临时演示公开样本但保留记录；新功能完成供体验，不接着做R5。
- [ ] Step 5：git diff --check、聚焦提交并推授权 fork，核对本地/fork/PR最终SHA、Draft、完整CI状态；若当前头未跑CI照实记录，GitGuardian不是完整CI。提供验收卡、技术说明及保存状态；完成后将本计划 scratch 留存必要证据到工作区日志并仅清理该计划workspace。

## 自审与执行记录

规格覆盖：§1–4 → Task 1；§5 → Task 2；§6 → Task 3；§2/7 → Task 4；§8–9 → Task 5。五项 Review Focus 均已有具名测试归属。公开快照、public_version、file状态与auth epoch在任务间使用同一签名；Tasks 2/3不能复制资格依赖；产生的合约在当次提交同步。执行采用 executing-plans；实际偏离在本计划专属 ledger 记录，按 spec 裁定，已完成任务不重复开发。
