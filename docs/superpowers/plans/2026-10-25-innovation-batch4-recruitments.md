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

## 实施记录与偏差(2026-10-25)

1. **迁移 revision 名**:`0033/0034` → `0044_recruitments` / `0045_recruitment_notifications`(≤32 字符,接 0043)。
2. **共享增量漏项由迁移测试暴露(22 红)**:招募联系限流规则、errors.py 的 SQLAlchemy 安全日志分支(批 1 曾按"独立改进"推迟——招募 error-privacy 测试证明它是隐私要求的一部分,现纳入)、DeliveryService 的 reminder resolver(IN_APP-only、tx2 抑制、fail-closed)、send_notification 注入、dispatch 的 event_types/queue 过滤、innovation_dev worker profile、clean_world 招募删除序列。全部补齐后 305 集成 + 1496 单元/worker 全绿。
3. **`_USERS`/DTOS 复用**:招募 schema 与公开服务全部改指 contact_schemas / accounts;`Text120/Text2000/RecruitmentStatus` 从共享或本文件恢复。
4. **主产品 worker 测试放宽**:dispatch 的 producer 签名断言纳入两个 keyword-only 过滤参数(§12 的"只传 correlation id"仍是默认调用形态)。
5. **前端适配**:contactFields/formUtils/ConfirmDialog 复用;业务冲突按 `code === "CONFLICT"`;通知 inbox 补招募提醒事件映射(PR 测试依赖)。
6. **R3×R2 交叉验证恢复**:批 3 裁剪的 review spec 段随招募端点回归。
7. **视觉**:招募 8 张基线首拍(pinned-font)+ 四家族 visual 全部进 make innovation-visual。
8. **共享会话 fixture 补齐(PR 机制移植)**:上游 fixtures 有 `openLoginForm`——登录页启动会触发单次 refresh 旋转,共享学生上下文里必须等旋转完成并把新 cookie 落盘,再清 `studentSessionContext` 标记;否则身份切换测试(165)会把 AUTHOR 的 cookie 写进共享状态文件,后续测试被迫表单登录。main 的 unified-login 版 `loginThroughUi` 无此机制,照 PR 原样移植(适配 main 的登录页形态),并把 `persistSession` 自动 fixture 收敛到同一 helper。
9. **登出语义适配(550/608)**:PR 的 `/logout` 页(含「确认退出登录」)属于上游 R1 导航交付物,main 的统一登录改造没有该路由;登出落点是 `/profile?tab=info` 的「退出登录」按钮(LogoutSection,含服务端撤销+epoch+跨标签页栅栏)。两条测试改走 main 落点,断言语义不变(私有草稿不跨账号、迟到联系响应不能复活)。
10. **视觉电池共享世界的种子幂等**:招募视觉(批 4 新增)与复审视觉在同一电池里用同一学生、均以 version 0 直写 owner-profile/资格;playlist 按字母序先跑招募,复审再写 version 0 → 409(批 4 前的电池在 e2e 腿即失败,该顺序从未跑过)。复审 spec 的种子改为先读当前 version 再写(profile 读 `{profile:…}` 嵌套、资格/授权读顶层),并注释共享世界契约;招募 spec 注释其先手身份。修复后 `make innovation-visual` 8/8 绿。
