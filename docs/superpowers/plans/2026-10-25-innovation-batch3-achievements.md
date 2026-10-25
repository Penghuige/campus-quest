# Innovation 合并批 3 — 成果 + 证据复审 实施计划

> 依据:`docs/architecture/innovation-boundary.md`、四批路线(owner 2026-10-24 批准)、批 1/2 先例。承接同一分支(`9368117` 为批 2 收官点,`make release-gate` EXIT=0)。

**Goal:** 合入 R2(成果与更新复审)与成果证据链:后端 16 文件(AchievementDraft/证据/AchievementReviewCase/ReviewConflict 模型 + 成果/证据/复审/运营复审四组服务与路由 + workflow_guards + 公开成果面)、4 张迁移(重领号 0040-0043)、通知事件与模板增量、证据上传存储方法;前端成果草稿/运营复审/公开成果三面 + `/innovation/reviews`(运营入口在此解锁)+ 项目详情成果页;e2e 与视觉基线首拍。不带入招募(批 4)。

**路由面(聚合入口追加):** `/ie/me/project-drafts/{id}/achievements*`、`/ie/me/achievement-evidence*`(证据上传/检查)、`/ie/ops/achievement-reviews*`(运营复审)、`/ie/achievements*`(公开成果)、管理员与运营面经 main 既有守卫。

## Global Constraints

- **守卫翻译**:本人面 `require_approved_student_actor`;运营复审面 `Student`(approved) + `OperationsGrant` 服务内校验(PR 语义);公开成果面 `require_active_actor`(含 PENDING_APPROVAL 浏览)。
- **迁移领号**:0028→`0040_achievement_drafts`、0030→`0041_evidence`、0031→`0042_achievement_review`、0032→`0043_review_notifications`(接 0039;revision id ≤32 字符与批 2 教训一致)。
- **models.py 恢复 AchievementDraft**(批 2 剥离的 51 行,原样放回)+ 新模型 evidence_models/review_models 聚合进 db/base。
- **共享增量**:notifications enums +`IE_ACHIEVEMENT_APPROVED`/`IE_ACHIEVEMENT_RETURNED`;notifications/templates.py 的板块模板(PR 增量原样);`ObjectStorage.create_evidence_upload_url`(Protocol + S3 实现 + fake);`operations_service._USERS` 改引 `accounts.USERS`(DRY,批 2 遗留)。
- **无新错误码**(复用 CONFLICT 等);如实施中发现需要,先登记 interfaces.md。
- **运营入口**:`/innovation/reviews` 页面落地后,StudentShell 的 capabilities 门控 + OPERATIONS_NAV 恢复(与前端 agent 批 2 落位的 `InnovationCapabilities` 组件接上)。
- **G19**:矩阵行(achievement-drafts/achievement-review 两份随迁;公开成果并入)、视觉基线首拍(review visual 5 张)、noskip 豁免、selector B 表核对、棘轮、契约工件。
- **通知与 worker**:批 3 无新 worker;复审通知走既有 `send_notification`(服务层 durable handoff,G9)。

## Review Focus

1. **复审不变量(G8/G15)**:首次审核通过后修改必须重审、旧通过版持续展示直到新版本通过、并发领取(claim)只有一个赢家、ReviewConflict 冲突回避——PR 的 races/review_boundaries 套件在真实 PG 通过。
2. **证据链 fail-closed(G1/G2/G4)**:上传→检查→READY 才能被成果引用;扫描失败关闭公开能力;真实 MinIO+ClamAV composition smoke。
3. **权限(G10)**:运营复审需 OperationsGrant 且不得复审本人成果;admin 面 + 网络守卫;公开成果面只暴露公开快照字段。
4. **隐私(G11)**:公开成果 DTO 白名单;项目/负责人资料不进公开面;通知内容不含联系方式。
5. **幂等/版本**:创建指纹、版本冲突 409、重复领取、回执重放不复活已退回/撤回的版本。
6. **导航完整性**:运营入口只在有授权时显示,指向可用的 `/innovation/reviews`。

---

### Task 1: 计划 + 矩阵随迁(test-matrix 两份 + 公开成果并入核对)
### Task 2: 共享增量(notifications enums/templates、object_storage evidence 方法、operations_service→accounts)
### Task 3: 迁移 0040-0043 + models 恢复/新增 + db 聚合 + verify-migrations 断言
### Task 4: 后端 16 文件 + 聚合挂载 + 守卫审计 + 审批语义测试
### Task 5: 后端测试迁移(achievement/evidence/review/operations 复审套件)
### Task 6: 契约 regen(零倒退)
### Task 7: 前端(成果草稿/复审/公开三面 + 运营入口解锁 + 页面)
### Task 8: e2e + 视觉基线首拍 + 工件 + pre-push → verify → release-gate

## 完成定义

- 全部 Task 完成;release-gate EXIT=0(fresh);守卫映射表 + 漂移审计三项进 PR 描述;分支不 push(待 owner 授权)。

## 实施记录与偏差(2026-10-25)

1. **workflow_guards 的层归属**:importlinter 首轮 BROKEN——它被 services 使用但被我放进 router 层。移入共享底层(models/schemas)后 14 契约 KEPT。
2. **公开成果的查看者状态放宽**:public_achievement_service 的 `_active` 仍要求 ACTIVE(写于审批制前),批 3 放宽为 ACTIVE 或 PENDING_APPROVAL(与 talent 公开面一致);PUBLIC 面 200、动作面 403 各有测试钉。
3. **clean_world 补批 3 序列**:review e2e 首次失败是 FK 违约(achievement drafts 引用 project drafts);按 PR 原顺序补齐 7 组删除(conflicts→cases→workflows→revision_evidence→revisions→evidence→drafts)后才绿。
4. **review spec 的招募段裁剪**:PR 的 R3×R2 交叉验证依赖招募端点(批 4),按范围裁掉并留注释;剩余成果链路(编辑/下架/更新复审)完整保留。
5. **e2e 电池的环境形态**:clean_world 修复前的失败运行会留下世界残留(公开成果泄漏进列表),运行前需 reset-test-db——与门禁的腿间重置一致。
6. **greenlet 新签名**:批 3 async 服务族在 full-suite 组合跑泄漏一个 beyond-EOF 端点(211)到 operations_service.py;按 owner 批准的 waiver 政策登记 exact-set 指纹(带 provenance),任何漂移会重新红。
7. **覆盖率基线时长**:批 3 测试量使 590s 超时,改 1800s;诊断性 `--cov` 运行必须用独立 COVERAGE_FILE(批 2 教训,已记入项目记忆)。
