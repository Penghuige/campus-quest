# Innovation 合并批 2 — 板块骨架 + 项目库 实施计划

> 依据:`docs/architecture/innovation-boundary.md`(边界公约)与四批路线(owner 2026-10-24 批准)。批 1(人才库)已收尾,边界分支 HEAD `283d8cc`,`make release-gate` EXIT=0。本批承接同一分支/worktree,同一边界规则。

**Goal:** 把 PR #43 的 R1(导航骨架)与项目库(项目草稿 / 负责人资料 / 负责人资格 / 运营授权)合入:后端 12 文件、4 张 `ie_*` 表迁移(重领号 0036-0039)、前端 14 模块 + 4 页面 + 真导航、e2e 与基线。不带入成果/证据/复审(批 3)与招募(批 4)。

**Architecture:** 依赖闭包已实测(PR b3adf14):models 仅依赖 Base;service/router 依赖 core + identity(enums/events,`service.py` 唯一直接触及 `identity.models.User` → 以 raw-table 锁替换,维持 main 的 ORM 契约);audit 由 owner/qualification/operations 使用;彼此间仅 `owner_service._require_student` 模块内共用。`workflow_guards.py` 依赖 review_models(批 3),本批不摘。

**路由面(全部经批 1 的 `aggregated.py` 挂载):** `/ie/me/project-drafts`、`/ie/me/owner-profile`、`/ie/me/owner-qualification`、`/ie/me/operations-grants`、`/admin/ie/owner-qualifications`、`/admin/ie/operations-grants`。

## Global Constraints

- **守卫翻译(审批制)**:本人管理面(草稿 CRUD、负责人资料、资格申请、本人授权查询)全部 `require_approved_student_actor`(批 1 的 `Student` 别名已是 approved);admin 面维持 `require_admin_actor` + `require_management_network_from_store`(main 既有,与 PR 一致)。产出守卫映射表。
- **ORM 契约**:`project-drafts service.py` 的 `select(User)` 行锁改为 raw-table(`talent_common.USERS` 同款),共享 `USERS` 抽取到 `innovation/accounts.py`(talent_common 与 public_talent_service 改引用,消除三份重复)。
- **迁移领号**:0025→`0036_innovation_project_drafts`(down `0035_talent_file_upload_contract`),0026→`0037_innovation_owner_profiles`,0027→`0038_innovation_operations_grants`,0029→`0039_innovation_owner_qualification`;链内 revision id 命名串风格;升/降/重升链测试。
- **错误码**:+`PROJECT_DRAFT_VERSION_CONFLICT`、`PROJECT_DRAFT_REQUEST_CONFLICT`、`OWNER_PROFILE_VERSION_CONFLICT`(interfaces.md 表格先登记 → 枚举 → 前端 errors.ts 镜像 → test_error_codes 冻结表)。
- **前端接入**:批 1 的 `InnovationShell` 禁用项翻新——项目库可用,成果/招募保留禁用;学生壳导航「创新创业」保持;「我的」页入口组加「我的项目」「负责人资料」(PR 结构,融入 main 现版)。
- **契约工件**:全部后端落位后 regen(先 fetch origin/main),同 PR。
- **importlinter**:innovation 契约的 layers 需追加本批文件(router 层 + service 层),contract 保持 KEPT。
- **G19**:新矩阵行(drafts/owner-profile/owner-qualification/operations 四份,随迁 PR 的 `docs/quality/test-matrix/innovation-*.md` 相关行)、e2e 视觉基线首拍、noskip 豁免(visual spec 天然)、selector B 表核对、棘轮 `--update`、契约工件。
- **门禁**:同批 1 —— pre-push → verify → release-gate(已是 CI 形态:relief + API_URL + 腿间纯净库)。

## Review Focus

1. **版本/幂等(G8/G15)**:草稿 CRUD 的 version 冲突 409、幂等回执、创建指纹不可变(PR 迁移注释即契约)——PR races 套件在真实 PG 通过。
2. **权限边界(G10)**:admin 面必须 network guard + admin actor;学生面 Student 守卫;所有者隔离(他人草稿 404)。
3. **审批制**:PENDING_APPROVAL 学生可浏览「创新创业」壳、不可创建/编辑草稿与申请资格——新增参数化测试。
4. **ORM 契约**:`make arch-lint` 的 "Identity ORM models stay behind the directory port" 保持 KEPT(本批不新增 ignore)。
5. **导航完整性**:壳内四个入口的真实跳转;「我的」页两个新入口;无死链(项目库落地页存在)。
6. **契约零倒退**:regen 后仅新增 `/ie/me/project-drafts*`、`/ie/me/owner-*`、`/admin/ie/*` 路径。

---

### Task 1: 计划与文档随迁

**Files:** 本计划;随迁 PR 的 `docs/quality/test-matrix/innovation-drafts.md`、`innovation-owner-profile.md`、`innovation-owner-qualification.md`、`innovation-operations.md`(自 b3adf14);AGENTS.md 无需改(边界文档已在批 1 引用)。

### Task 2: 共享增量

**Files:**
- Modify `backend/app/core/error_codes.py`(+3 码,注册顺序与 interfaces.md 表一致)
- Modify `docs/architecture/interfaces.md`(表格 + Innovation module 小节补批 2 端点组)
- Modify `backend/tests/unit/core/test_error_codes.py`(冻结表 +3)
- Create `backend/app/modules/innovation/accounts.py`(`USERS` raw table;talent_common 与 public_talent_service 改引用)
- Modify `frontend/src/lib/errors.ts`(+3 码镜像)

### Task 3: 迁移 + 模型

**Files:** Create `backend/alembic/versions/0036_innovation_project_drafts.py`、`0037_innovation_owner_profiles.py`、`0038_innovation_operations_grants.py`、`0039_innovation_owner_qualification.py`(自 PR 对应 0025/0026/0027/0029,改链);Create `backend/app/modules/innovation/models.py`(4 模型);Modify `db/base.py`(聚合 +1 import);Modify `scripts/verify-migrations.sh`(新表/唯一索引断言)。

### Task 4: 后端本体

**Files(自 b3adf14,import 适配):** `schemas.py`、`service.py`(User→raw table)、`router.py`(project-drafts)、`owner_schemas.py`、`owner_service.py`、`owner_router.py`、`qualification_schemas.py`、`qualification_service.py`、`qualification_router.py`、`operations_schemas.py`、`operations_service.py`、`operations_router.py`;`aggregated.py` +6 路由挂载。

### Task 5: 审批语义 + 架构契约 + 文档门禁

新增 `backend/tests/integration/innovation/test_project_approval_semantics.py`(PENDING_APPROVAL:可浏览壳相关公开面(无)、草稿/资料/资格/授权查询全 403);importlinter layers 追加本批文件。

### Task 6: 测试迁移

PR 集成/单测:`test_project_drafts.py`、`test_owner_profiles.py`、`test_owner_qualification.py`、`test_operations_grants.py`、`test_qualification_actor_recheck.py` + 单测 `test_*_schemas.py`;适配规则同批 1(account 夹具、跨批引用删除/内联、pytestmark)。

### Task 7: 契约 regen

同批 1(先 fetch,再 dump + api:types:snapshot;零倒退 diff 校验)。

### Task 8: 前端

**Files(自 b3adf14,import 适配):** `api.ts`、`draftForm.ts`、`ProjectDraftsView.tsx`、`ownerApi.ts`、`ownerForm.ts`、`OwnerProfileView.tsx`、`qualificationApi.ts`、`qualificationForm.ts`、`OwnerQualificationPanel.tsx`、`OwnerQualificationAdmin.tsx`、`operationsApi.ts`、`operationsForm.ts`、`OperationsGrantAdmin.tsx`、`InnovationNavigation.tsx`;页面 `(student)/profile/project-drafts/page.tsx`、`(student)/profile/project-drafts/[projectId]/achievements/page.tsx`(批 3 依赖的占位壳?——按 PR 原样落,批 3 补内容)、`(student)/profile/owner-profile/page.tsx`、`admin/owner-qualifications/page.tsx`、`admin/innovation-operations/page.tsx`;`InnovationShell` 翻新(项目库可用);「我的」页入口组;组件单测随迁。

### Task 9: e2e + 基线 + 门禁

`innovation-drafts.spec.ts`、`innovation-owner-profile.spec.ts`、`innovation-owner-qualification.spec.ts`、`innovation-operations.spec.ts`、`innovation-navigation.spec.ts`(+visual spec)与基线首拍;noskip 豁免;selector B 表;棘轮;pre-push → verify → release-gate。

## 完成定义

- 全部 Task 勾选;release-gate EXIT=0(fresh);守卫映射表 + 漂移审计三项进 PR 描述;分支不 push(待 owner 授权)。
