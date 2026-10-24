# Innovation 合并批 1 — 人才库(Talent)最小闭包 实施计划

> **For agentic workers:** 按本计划逐 Task 实施;步骤用 checkbox 跟踪。写测试或动测试基建前先调用 `campusquest-test-process` skill(门禁矩阵与栈借用协议以它为准)。边界规则见 `docs/architecture/innovation-boundary.md`(同 PR 入库)。

**Goal:** 把 PR #43(YeZesen0121/campus-quest @ b3adf14)的 R4/R4.1 人才库以最小闭包合入 main:后端 talent 本体 + 其共享层增量、前端组件与页面、e2e 与基线、契约工件。不带入项目库/成果/招募/资格/运营(批 2-4)。

**Architecture:** talent 在 PR 内零业务依赖(数据层 FK 只挂 `users.id`;代码只借 3 个符号源,全部内联)。共享层只做扩展点登记(scanner 端口、storage 3 方法、限流 1 规则、错误码 1 个、配置 3 项);`main.py` 经板块聚合入口挂载净增 2 行;迁移重新领号接 main head。来源对象:`git show b3adf14199e4a1074c9d01f719ff06cafd465390:<path>`(FETCH_HEAD 已在本仓对象库)。

**Tech Stack:** FastAPI + SQLAlchemy 2 async + Alembic + Redis + MinIO(S3)+ ClamAV(新);Next.js client islands + Playwright。

**Spec:** 来源 spec `docs/superpowers/specs/2026-10-12-innovation-talent-design.md`(随迁入库,Task 1)+ 本计划。合并决策记录:与 owner 的 2026-10-23/24 讨论(四批路线、批 1 一级导航+诚实占位、docs 门禁测试化、聚合路由、待审批学生可浏览不可发布、ClamAV 沿用)。

## Global Constraints

- **守卫语义**:talent 维护/发布/撤回端点用 `require_approved_student_actor`(PENDING_APPROVAL 学生 403 `APPROVAL_PENDING`);浏览端 `Viewer = require_active_actor`(放行 PENDING_APPROVAL);公开可见条件保持"主人 ACTIVE STUDENT"。产出守卫映射表(PR 描述 + 本计划 Task 6)。
- **共享文件只叠加**:对 object_storage/rate_limit/error_codes/config/main.py/db/base 的每一行改动都是新增;`main.py` 保留 main 的 docs 门禁骨架(docs_disabled 逻辑不得丢失)。
- **迁移领号**:`0035_innovation_talents` → `0034_innovation_talents`(down_revision=`"0033_generic_platform"`);`0036_talent_file_upload_contract` → `0035_talent_file_upload_contract`(down_revision=`"0034"`);文件内 revision id 同步改名;升级/降级/重升级链测试。
- **聚合路由**:`modules/innovation/__init__.py` 暴露 `router`(内部 include talent 三件套);`app/main.py` 净增:1 行 import、1 行挂载、1 行 `InnovationPrivacyMiddleware`。
- **符号内联**:`innovation/dependencies.py` 承载 `Database`/`Student`/`Viewer` 别名与 `_private_response`(来源 innovation/router.py、public_achievement_router.py,共 ~9 行);`innovation/contact_schemas.py` 承载 `ContactChannel`/`ContactInput`(来源 recruitment_schemas.py ~41 行,依赖全在 main)。批 4 招募合并时复用,不重复抽取。
- **契约工件**:全部代码落位后 fetch origin/main → `dump_openapi.py` + `npm run api:types:snapshot` regen,同 PR;绝不采用 PR 自带快照。
- **importlinter**:pyproject 增加 `innovation: routers above services` layers 契约(talent_router : talent_file_router : public_talent_router 在上,talent_service : talent_file_service : public_talent_service 在下);前端 depcruise 确认 `features/innovation/` 新路径被规则覆盖。
- **mypy 清零**:批 1 范围内 25 个已知缺口中属于本批的(Settings clamd×3、ErrorCode TALENT_CONTACTS_UNAVAILABLE、ObjectStorage talent 方法×2 + StoredObjectTooLargeError)必须清零;`no-any-return`×3 随 `read_bounded_object` 合成后消失,若仍在则修 PR 代码类型。
- **测试纪律**(campusquest-test-process):新集成测试文件带 `pytestmark`;talent 上传意图 201/200 幂等带跨连接持久性断言(独立 `AsyncSession` 复核行落库);并发/版本竞态跑真实 PG(G15);composition smoke 覆盖真实 MinIO + ClamAV(G1/G2/G18);本地跑任何测试前按栈借用协议通告 campus reviewer 会话。
- **G19 工件同 PR**:矩阵文档 `docs/quality/test-matrix/innovation-talent.md` 随迁并按"预期先于测试检索"方法论核对;13 张 talent 基线首拍(pinned-font);noskip 豁免 4 条;e2e-selector-contract.md B 表;覆盖率棘轮走 `--update` 带理由;`interfaces.md` 登记新错误码;`docs/operations/release-checklist.md` 加 ClamAV。
- **G3**:搬入代码不得有占位符;`InnovationShell` 的"项目库/成果/招募 即将开放"是产品诚实占位(可见文案),不是代码占位,允许。
- 提交信息 plain conventional,无 AI trailer;不 push(推送/PR 另行授权)。

## Review Focus

1. **隐私面(G11)**:公开 DTO 字段白名单——无 user_id/学号/object key/上传 URL/联系值;列表 DTO 是摘要子集;联系值仅 `POST /ie/talents/{id}/contacts` 响应;文件内容仅授权代理端点。测试钉:PR 的 `test_public_talents.py::test_public_projection_excludes_private_sentinels` 必须原样通过。
2. **授权读取与审计(G12)+ fail-closed(G5)**:联系/文件读取先审计后给值,审计失败不给;Redis 不可用 → 503 `TALENT_CONTACTS_UNAVAILABLE`;文件下载最终授权事务排序(关闭/撤回先提交则不输出)。
3. **守卫翻译**:PENDING_APPROVAL 学生能浏览列表/详情/标签,维护端点 403;被禁用查看者中性 404。测试钉:新增参数化测试(任务 6)。
4. **版本/幂等/竞态(G8/G15)**:双窗口旧 version 409、幂等回执重放不复活、文件撤回不可逆——PR 的 races 套件在真实 PG 通过。
5. **共享层 diff 审计**:对 §Global Constraints 第 2 条所列文件逐 diff,确认只有登记行(边界文档 §8 第 1 项)。
6. **契约无倒退**:regen 后 diff 里只有 `/ie/talents*`、`/ie/me/talent-profile*`、`/ie/talent-tags` 新增,main 既有端点零变化。
7. **组合边界(G1/G2/G18)**:composition smoke 在真实 MinIO + ClamAV 组合下通过;CI visual job 与 release-gate 覆盖。

---

### Task 1: 规约与文档入库

**Files:**
- Create: `docs/architecture/innovation-boundary.md`(已写)
- Create: 本计划
- Create(随迁,自 b3adf14 原样): `docs/superpowers/specs/2026-10-12-innovation-talent-design.md`、`docs/superpowers/plans/2026-10-12-innovation-talent-plan.md`、`docs/quality/test-matrix/innovation-talent.md`、`docs/quality/innovation-talent-review.md`、`docs/quality/innovation-talent-r4-1-review.md`、`docs/quality/innovation-talent-verification.md`、`docs/quality/innovation-talent-r4-1-verification.md`(评审/验证记录作为板块证据链)
- Modify: `AGENTS.md` — 在 "Generated code and external references" 节后加一行:板块边界与协作规则见 `docs/architecture/innovation-boundary.md`(板块改动必读)

**Steps:**
- [ ] 随迁文档自 `git show b3adf14:<path>` 原样落盘(不改写内容,保留作者证据链)
- [ ] AGENTS.md 加引用行
- [ ] 静态核对:矩阵文档的预期列与来源 spec 对得上(方法论:预期先于测试)

### Task 2: 后端共享层增量(integrations + core + fakes)

**Files:**
- Create: `backend/app/integrations/evidence_scanner.py`(48 行,自 b3adf14 原样)、`backend/app/integrations/evidence_scanner_clamd.py`(109 行,原样)
- Modify: `backend/app/integrations/object_storage.py` — +`StoredObjectTooLargeError(ValueError)`、+`read_bounded_object(*, object_key: str, max_bytes: int) -> StoredObject`、+`create_talent_file_upload_url(*, profile_id: UUID, content_type: str, content_length: int, expires_in: timedelta) -> UploadUrl`(签名取自 b3adf14,追加到既有 Protocol)
- Modify: `backend/app/integrations/object_storage_s3.py` — +上述三方法的 S3 实现(`talent/{profile_id}/{uuid4()}` key、write-once、长度/MIME pinning、有界读)
- Modify: `backend/app/integrations/rate_limit.py` — +`"ie:talent-contacts": RateLimitRule(bucket="ie:talent-contacts", limit=30, window_seconds=60)` 进 `RATE_LIMIT_RULES`
- Modify: `backend/app/core/error_codes.py` — +`TALENT_CONTACTS_UNAVAILABLE`
- Modify: `backend/app/core/config.py` — +`clamd_host`("127.0.0.1")/`clamd_port`(3310)/`clamd_timeout_seconds`(30)与 host 校验
- Modify: `backend/app/integrations/errors.py` 若 PR 版有新增(核对后决定)
- Modify: `backend/tests/fakes/integrations.py` — +Fake scanner(记录调用+`fail_on` 编程)、FakeObjectStorage +talent 上传/有界读支持(镜像真实签名契约)
- Modify: `docs/architecture/interfaces.md` — 登记 `TALENT_CONTACTS_UNAVAILABLE` 与 `/ie/talents*` 端点组、scanner 端口
- Test: 搬迁 PR 的 `backend/tests/unit/integrations/` 中 scanner/storage 相关用例(如有)+ 为新 Protocol 方法补 fake 契约测试

**Steps:**
- [ ] 先跑 `uv run pytest tests/unit/integrations -q` 记录基线绿
- [ ] 逐文件叠加增量(共享文件只加行,不动既有行)
- [ ] ruff format/check + mypy(新文件与改动文件)三绿
- [ ] 单测:scanner fake 行为、storage fake 镜像契约(client_headers、pinned_content_length、write-once 412)

### Task 3: 迁移领号 + 模型 + db 聚合

**Files:**
- Create: `backend/alembic/versions/0034_innovation_talents.py`(自 b3adf14 `0035_innovation_talents.py`,改 `revision="0034_innovation_talents"`、`down_revision="0033_generic_platform"`;内容原样:7 张 `ie_talent_*` 表 + 标签种子 + FK 只挂 users)
- Create: `backend/alembic/versions/0035_talent_file_upload_contract.py`(自 `0036`,改链;内容原样:add_column + `ie_talent_file_guard` 触发器)
- Create: `backend/app/modules/innovation/__init__.py`(占位,Task 5 补聚合 router)
- Create: `backend/app/modules/innovation/talent_models.py`(自 b3adf14 原样)
- Modify: `backend/app/db/base.py` — 底部 +`from app.modules.innovation import talent_models as innovation_talent_models  # noqa: E402, F401`
- Test: `backend/tests/integration/innovation/test_talent_migrations.py`(新写:干净库 upgrade→head、head→`0033` downgrade→re-upgrade、`alembic check` 无漂移)

**Steps:**
- [ ] 新迁移测试先写先跑 → RED(表不存在)
- [ ] 落迁移与模型,db/base 聚合
- [ ] 隔离库跑迁移链测试 GREEN(栈借用协议:通告 campus reviewer)
- [ ] `uv run alembic check` 无 autogenerate 漂移

### Task 4: talent 后端本体 + 符号内联

**Files:**
- Create: `backend/app/modules/innovation/dependencies.py` — `Database = Annotated[AsyncSession, Depends(get_db_session)]`、`Student = Annotated[Actor, Depends(require_active_student_actor)]`、`Viewer = Annotated[Actor, Depends(require_active_actor)]`、`_private_response`(来源:b3adf14 innovation/router.py + public_achievement_router.py)
- Create: `backend/app/modules/innovation/contact_schemas.py` — `ContactChannel`/`ContactInput`(来源:b3adf14 recruitment_schemas.py;`EmailVerificationService._normalized_email`/`normalize_phone` 等 import 改指 main 的 identity 模块)
- Create(自 b3adf14 原样,仅 import 路径改指上述两文件): `talent_schemas.py`、`talent_common.py`、`talent_service.py`、`talent_router.py`、`talent_file_schemas.py`、`talent_file_service.py`、`talent_file_router.py`、`public_talent_service.py`、`public_talent_router.py`、`privacy.py`(21 行 ASGI middleware,原样)

**Steps:**
- [ ] 落 dependencies.py + contact_schemas.py(两个内联源)
- [ ] 10 个 talent 文件落盘,import 从 `innovation.router`/`innovation.public_achievement_router`/`innovation.recruitment_schemas` 改指 `innovation.dependencies`/`innovation.contact_schemas`
- [ ] 聚合前的静态检查:ruff + mypy 对 `app/modules/innovation/` 全绿(Task 2 增量已清掉 Settings/ErrorCode/ObjectStorage 缺口)

### Task 5: 聚合路由 + main.py + docs 门禁测试 + 架构契约

**Files:**
- Modify: `backend/app/modules/innovation/__init__.py` — 聚合 `router = APIRouter()` 并 include talent_router/talent_file_router/public_talent_router 三件(统一 `prefix="/api/v1"` 下挂载时保持 `/ie/...` 路径不变)
- Modify: `backend/app/main.py` — +import 1 行、`app.include_router(innovation_router.router, prefix="/api/v1")` 1 行、`app.add_middleware(InnovationPrivacyMiddleware)` 1 行;**其余(main 的 docs 门禁、e2e relief、既有注释结构)零改动**
- Test: `backend/tests/unit/core/test_app_docs_gate.py`(新写:production settings 下 `create_app()` 的 `app.docs_url/app.redoc_url/app.openapi_url` 全 None;非 production 保留)
- Modify: `backend/pyproject.toml` — +`[[tool.importlinter.contracts]]` innovation layers 契约
- Test: `make arch-lint` 通过(含新契约)

**Steps:**
- [ ] docs 门禁测试先写先跑(对当前 main 的 create_app)→ GREEN(锁定现状)
- [ ] 聚合 router + main.py 挂载
- [ ] `make arch-lint` GREEN;`make arch-map` 再生成,as-built 图出现 innovation 节点且边符合预期(→ identity/audit/integrations,无 → tasks/points)
- [ ] 契约快照 regen 预检(正式 regen 在 Task 8)

### Task 6: 守卫翻译 + PENDING_APPROVAL 语义测试

**Files:**
- Modify: talent 三个 router 的维护端点依赖(`Student` 别名的实现换 `require_approved_student_actor`;浏览端 `Viewer` 保持 `require_active_actor`)
- Create: `backend/tests/integration/innovation/test_talent_approval_semantics.py`(新写:PENDING_APPROVAL 学生可 GET /ie/talents、/ie/talents/{id}、/ie/talent-tags;PUT/publish/hide/revoke/files 全 403 `APPROVAL_PENDING`;公开列表不含 PENDING_APPROVAL 主人——由"发布需 approved"推导,断言现有可见性条件不变)
- 产出:守卫映射表(端点→守卫→账户状态预期),进 PR 描述

**Steps:**
- [ ] 语义测试先写先跑 → RED(旧守卫放行 PENDING_APPROVAL)
- [ ] 换守卫 → GREEN
- [ ] 全量 talent 集成测试回归(确认无既有用例依赖旧语义;PR 自带用例中如有以 ACTIVE 学生为基准的,不受影响)

### Task 7: 后端测试搬迁

**Files(自 b3adf14 原样,夹具路径按需适配):**
- `backend/tests/integration/innovation/talent_support.py`、`test_talent_lifecycle.py`、`test_talent_races.py`、`test_talent_files.py`、`test_talent_file_races.py`、`test_talent_contacts.py`、`test_talent_public_file_races.py`、`test_public_talents.py`、`test_talent_composition_smoke.py`、`test_talent_world_cleanup.py`
- `backend/tests/unit/innovation/test_talent_schemas.py`、`test_talent_file_schemas.py`

**Steps:**
- [ ] 每个集成文件带 `pytestmark = pytest.mark.integration`(pytestmark 陷阱)
- [ ] 核对上传意图 201 的跨连接持久性断言存在;缺失则补(独立 `AsyncSession` 复核 TalentFile 行;真提交行 cleanup 由测试负责)
- [ ] 借栈通告后全量跑 `uv run pytest tests/integration/innovation tests/unit/innovation -q`(真实 PG)
- [ ] composition smoke 需 MinIO + ClamAV:`docker compose -p campusquest-evidence -f infra/docker-compose.clamav.yml up -d --wait` 后跑(Task 10 同栈)
- [ ] 契约对账:`check_test_matrix.py` 对 `innovation-talent.md` 通过

### Task 8: 契约 regen + 静态三绿

**Steps:**
- [ ] `git fetch origin main` 确认基线无新漂移
- [ ] `uv run python scripts/dump_openapi.py` → snapshot diff 只含 `/ie/` 新端点组
- [ ] `npm run api:types:snapshot` → schema.d.ts 同步
- [ ] ruff format --check + ruff check + mypy(backend 全量)三绿;mypy 25 缺口中批 1 范围清零
- [ ] `make pre-push` 全绿(契约新鲜度列)

### Task 9: 前端本体

**Files:**
- Modify: `frontend/src/lib/accessToken.ts` — +`onAuthEpochChange(listener)`(+内部 `advanceAuthEpoch`),重放到 main 重写后的结构(与既有 epoch 栅栏共存,不动既有导出)
- Create: `frontend/src/features/innovation/contactFields.ts` — `CONTACT_LABELS`、联系渠道输入配置(来源:b3adf14 recruitmentPresentation/recruitmentForm 的相关符号;批 4 复用)
- Create: `frontend/src/features/innovation/formUtils.ts` — `codePointCount`(来源:draftForm)
- Create(自 b3adf14,import 改指上述共享文件): `talentApi.ts`、`talentForm.ts`、`talentQuery.ts`、`talentPresentation.ts`、`talentRecovery.ts`、`TalentOverview.tsx`、`TalentFields.tsx`、`TalentFiles.tsx`、`TalentContacts.tsx`、`TalentPreviewContent.tsx`、`TalentPreview.tsx`、`TalentProfileView.tsx`、`TalentTagSuggestion.tsx`、`PublicTalentsView.tsx`(`RecruitmentConfirm` → 抽为 `ConfirmDialog.tsx` 通用确认组件,来源 b3adf14)
- Create: `frontend/src/app/(campus)/layout.tsx` + `frontend/src/features/innovation/InnovationShell.tsx` — 极简板块壳(复用 WorkspaceSidebar;创新导航:人才库 + "项目库/校内成果/正在招募"诚实占位文案;批 2-4 逐步替换)
- Create: `frontend/src/app/(campus)/innovation/talents/page.tsx`、`innovation/talents/[id]/page.tsx`
- Create: `frontend/src/app/(student)/profile/talent-profile/page.tsx`
- Modify: `frontend/src/app/(student)/_components/StudentShell.tsx` — 导航数组 +"创新创业" 一级项(`/innovation`)
- Modify: `frontend/src/app/(student)/profile/page.tsx` — +"我的人才档案"入口

**Steps:**
- [ ] 基础符号先落(accessToken 订阅、contactFields、formUtils、ConfirmDialog)
- [ ] 14 个 talent 组件落盘并改 import
- [ ] 页面 + 壳 + 导航入口
- [ ] `npm run typecheck && npm run lint && npm run check:css` 三绿;`npm run arch:lint` 确认新路径被 depcruise 覆盖(缺规则则补)
- [ ] 组件单测(自 b3adf14 `src/__tests__/talent-*.test.ts` 五件随迁适配)

### Task 10: e2e + 基线 + 豁免 + 全链门禁

**Files:**
- Create(自 b3adf14): `frontend/e2e/innovation-talents.spec.ts`、`innovation-talents-visual.spec.ts`、`talent-helpers.ts` + `innovation-talents-visual.spec.ts-snapshots/`(13 张基线,**首拍**入库——如搬入基线与实际渲染不符,按首拍流程重拍并在 PR 说明,禁止调容差)
- Modify: `frontend/e2e/noskip-exemptions.mjs` — +talent visual 4 条豁免(沿用 PR 理由:talent 像素与 axe 在独立 pinned-font stage 跑)
- Modify: `docs/quality/e2e-selector-contract.md` — B 表 +talent spec 依赖的选择器
- Modify: `Makefile` — +`release-evidence-scanner` 目标;release-gate 依赖它;集成测试命令加 `CQ_EVIDENCE_SCAN_SMOKE=1`;+innovation-talents-visual 进 release-gate
- Create: `infra/docker-compose.clamav.yml` + `infra/clamav/`(自 b3adf14)
- Modify: `infra/env.example` — +clamd 三项
- Modify: `docs/operations/release-checklist.md` — +ClamAV 服务
- Modify: `backend/coverage-ratchet.json` / `frontend/coverage-ratchet.json` — 搬入测试后按 `--update` 流程更新地板,理由:talent 套件增量

**Steps:**
- [ ] 借 3000 栈(通告 campus reviewer → 停常驻栈 → 用 → 还)
- [ ] `CQ_E2E=1` 跑 `innovation-talents.spec.ts` 全绿(no-skip 断言生效)
- [ ] `CQ_E2E=1 CQ_VISUAL=1 CQ_E2E_FIXED_LABELS=1` 跑 visual spec;13 张基线按首拍纪律入库;`frontend/scripts/assert-e2e-no-skips.mjs` 通过
- [ ] `make pre-push` → `make verify`(compose stack)→ `make release-gate`(含 ClamAV 组合与 talent visual)
- [ ] 棘轮更新提交独立说明;矩阵/豁免/B 表/接口文档随迁齐全
- [ ] 最终 diff 自查:无 secrets、无调试残留、无 TODO 占位、无 PR 自带契约工件

## 完成定义

- 全部 checkbox 勾完;`make pre-push` + `make verify` + `make release-gate` 当轮 fresh 日志;
- Review Focus 7 项各有测试或 diff 证据;
- 守卫映射表 + 漂移审计三项(边界文档 §8)填入 PR 描述;
- 分支不 push;PR 与 reviewer 派发待 owner 指示。
