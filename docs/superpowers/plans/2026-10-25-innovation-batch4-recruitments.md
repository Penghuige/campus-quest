# Innovation 合并批 4 — 招募(最后一批) 实施计划

> 依据:边界公约、四批路线(owner 批准)、批 1-3 先例。承接分支(`1ebe5e3` 为批 3 收官,`make release-gate` EXIT=0)。本批完成后四批全收。

**Goal:** 合入 R3 招募:后端 9 文件(招募模型/服务/路由、公开招募面、公开项目面、到期调度器)+ worker `schedule_recruitments` + 迁移 0044-0045 + 通知事件/模板/配置;前端招募三面(编辑器/工作台/公开浏览)+ 项目详情页 + 招募确认/联系方式 + 壳与入口解锁(正在招募);e2e(功能+视觉基线)+ 恢复批 3 裁剪的 R3×R2 交叉验证。

**路由面:** `/ie/me/project-drafts/{id}/recruitments*`、`/ie/recruitments*`(公开)、`/ie/projects/{id}`(公开项目详情)——聚合入口追加。

## Global Constraints

- **守卫**:本人面 `require_approved_student_actor`;公开招募/项目面 `require_active_actor`(含 PENDING_APPROVAL 浏览);更新公开服务的 viewer 状态检查(批 1-3 同款)。
- **迁移领号**:0033→`0044_recruitments`、0034→`0045_recruitment_notifications`(接 0043;`down_revision` 精确串)。
- **共享增量**:`NotificationEventType.IE_RECRUITMENT_CONFIRMATION_DUE` + 模板(PR 原样,含 `manage_path`);Settings +`recruitment_scan_interval_seconds`/`recruitment_scan_batch_limit`(含 batch_limit 校验器);`db/base.py` 聚合 recruitment_models。
- **contact_schemas 复用**:落 `recruitment_schemas.py` 时删除其中的 ContactChannel/ContactInput 定义,改 `from app.modules.innovation.contact_schemas import ...`(批 1 抽取的归宿)。
- **worker**:`schedule_recruitments` job + celery beat 注册(按 main 的 celery_app 现状接入;worker 只做调度,状态转换在服务层)。
- **导入适配**:`innovation.router` 的依赖 → `innovation.dependencies`。
- **前端接入**:InnovationShell「正在招募」解锁;项目库列表页从最小落地页升级为 PublicProjectsView;`/innovation/projects/[id]` 详情页;招募编辑/工作台/公开页;`(student)/profile/project-drafts/[projectId]/recruitments/page.tsx`;批 2 的「招募管理(后续批次开放)」恢复为链接。
- **G19**:矩阵行(recruitment 随迁 + 公开项目并入)、视觉基线首拍、noskip、selector B 表、棘轮(clean baseline)、契约工件。
- **恢复批 3 裁剪**:review spec 的 R3×R2 交叉段 + ProjectDraft 页的招募链接。

## Review Focus

1. **招募授权联系**——沿用 talent 的授权联系纪律(限流桶 `ie:recruitment-contacts`、fail-closed、审计先于给值)。
2. **到期调度(G8/G9)**:调度器扫描到期招募 → 通知;幂等(重复扫描不重复通知);worker 不复制状态机。
3. **权限(G10)**:发布需负责人资格/项目权限;运营免审但受权限约束;非所有者 404。
4. **公开面(G11)**:公开招募 DTO 白名单;联系值仅授权读取;项目详情聚合已核实成果与有效招募。
5. **跨模块不变量(批 3 裁剪恢复)**:关闭招募不影响已通过成果与项目可见性。

---

### Task 1: 计划 + 矩阵随迁
### Task 2: 共享增量(通知事件/模板、配置、db 聚合)
### Task 3: 迁移 0044-0045 + recruitment_models + verify-migrations
### Task 4: 后端 9 文件 + worker + 聚合挂载 + 守卫/语义测试
### Task 5: 后端测试迁移
### Task 6: 契约 regen
### Task 7: 前端(招募三面 + 项目详情 + 入口解锁 + 恢复批 3 裁剪)
### Task 8: e2e + 基线首拍 + 恢复交叉段 + 工件 + pre-push → verify → release-gate

## 完成定义

全部 Task 完成;release-gate EXIT=0(fresh);守卫映射 + 漂移审计进 PR 描述;四批全收;分支不 push(待 owner 授权)。
