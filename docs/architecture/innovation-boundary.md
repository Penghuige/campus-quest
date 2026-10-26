# Innovation 板块边界与协作公约

适用对象:主仓库(Penghuige/campus-quest 的 main)与创新创业板块开发线(YeZesen0121 fork,PR #43 及后续)。本文是双方共同的边界规则与协作流程;owner 批准后对两侧同时生效。规则的依据是仓库既有的分层标准(docs/quality/backend-engineering.md §1/§2/§20)与架构契约门禁(`make arch-lint`),本文不发明新原则,只把它们落到板块场景。

## 1. 单一事实源

main 是唯一事实源与发布源;fork 是板块开发线。合并只往一个方向流:fork → main。每个合并 PR 的 head 必须已包含最近的 origin/main(建议 fork 每轮交付前、至少每周 merge 一次 upstream main)——冲突由板块侧在自己分支消化,不塞给 main 的 reviewer。main 侧不依赖 fork 自觉:§8 的漂移审计是兜底防线。

## 2. 住址(板块私有范围)

板块代码只住以下地址,越出住址的 diff 默认打回:

| 层 | 住址 |
| --- | --- |
| 后端业务 | `backend/app/modules/innovation/` |
| 后端测试 | `backend/tests/{integration,unit}/innovation/` |
| 前端组件 | `frontend/src/features/innovation/` |
| 前端页面 | `frontend/src/app/(campus)/innovation/**` 与板块入口相关文件 |
| e2e | `frontend/e2e/innovation-*.spec.ts`、`talent-helpers.ts` 及其快照目录 |
| 运维 | `infra/`(ClamAV 组合等板块引入的栈) |

数据库表使用 `ie_` 前缀。板块与校园任务业务(tasks/points/community/submissions/rankings)在模块层零依赖——这是两侧互不腐蚀的结构保证,由 arch-lint 契约强制(§8)。

## 3. 共享层:只走扩展点,不改行为

板块对共享层(identity/audit/core/db/integrations/前端 lib 与 shell)的合法改动**只有登记**,没有修改:

| 允许的登记(追加行) | 登记处 |
| --- | --- |
| 新错误码 | `core/error_codes.py` 枚举 + `docs/architecture/interfaces.md` + 前端 `lib/errors.ts` 镜像 |
| 新限流规则 | `integrations/rate_limit.py` 的 `RATE_LIMIT_RULES` |
| 新配置项 | `core/config.py` Settings(含生产 guard) |
| 新存储/外部端口方法 | `integrations/object_storage.py` 等 Protocol + 真实适配器 + 测试 fake 三件套 |
| 新通知事件类型 | `notifications/enums.py` |
| 模型聚合 | `db/base.py` 底部 import |
| 路由挂载 | `main.py` 经板块**聚合入口**挂载(一次一行,不得平铺子路由) |

**禁止**:修改共享文件的既有行为——守卫语义、既有接口签名、既有错误码含义、登录/身份流程、限流算法。共享层语义演进(如审批制)由 main 决定,板块跟随(§7)。判据:合并 diff 中对共享文件的每一行改动都应是新增注册;删除或修改既有行需要在该 PR 描述中专项说明理由。

## 4. 公用资产

板块引入但归属公用层的资产(板块只是首个消费者,主项目后续功能按普通公用端口消费):

- `integrations/evidence_scanner.py`(扫描端口)+ `evidence_scanner_clamd.py`(ClamAV 适配器)+ 测试 fake;
- `ObjectStorage` 的有界读取(`read_bounded_object`)与写一次上传方法。

板块新需要的公共设施,先在 main 落成公用端口,板块再消费;不得在板块目录里养"私房基础设施"再反向泄漏。

## 5. 迁移领号

fork 内部迁移用临时名(如 `ie_r4_talents`),**不占用正式编号**;合并进 main 时统一领号(接当前 head,使用 main 的命名风格)并在 PR 中跑升级/降级链测试。主仓库并行开发期间迁移所有权串行(AGENTS.md 既有规则)。

## 6. 契约与基线纪律

- fork 不维护自己的 `openapi.snapshot.json` / `schema.d.ts`;合并 PR 内从 main 当前面 regen(先 fetch origin/main,防止 API 面静默倒退)。router docstring 改动同样翻转快照。
- fork 永不重拍 main 既有像素基线;板块新界面基线在合并 PR 中按 pinned-font 纪律首拍。重拍掩盖失败是阻塞项。

## 7. 语义同步义务

main 的身份与权限语义演进时(如注册审批制引入 `PENDING_APPROVAL`),板块全部端点的守卫映射必须同步翻译并随下一批合并落地。每轮合并 PR 附守卫映射表(端点 → 守卫 → 对各账户状态的预期行为),reviewer 逐行核对。当前基线:板块浏览面 `require_active_actor`(含 PENDING_APPROVAL),动作面(发布/维护/撤回)`require_approved_student_actor`。

## 8. 每轮合并的漂移审计(main 侧兜底)

不依赖 fork 配合,每个板块合并 PR 必须通过三项机械检查:

1. **共享文件 diff 审计**:对 §3 清单内的每个共享文件,确认 diff 只含扩展点登记;
2. **守卫映射表核对**(§7);
3. **门禁**:`make arch-lint`(含板块的 importlinter/depcruise 契约,见下)+ 契约工件 regen 校验 + `make pre-push`。

板块合并时必须同步补齐:`modules/innovation` 的 importlinter layers 契约(routers above services,与既有模块同款)与前端 depcruise 对新路径的覆盖——没有契约的模块等于架构门禁盲区。

## 9. 交付与验收

沿用仓库既有制度:发码类 202 响应带跨连接持久性断言;集成测试文件必带 `pytestmark`;G19 测试工件(矩阵行、豁免、基线、选择器契约、棘轮地板、契约工件)随特性 PR 同 ship;双审 + owner 终审;owner 以 fresh gate 输出验收。

## 10. 收编

板块进入稳定期(导师库、治理落地)后,fork 退役、直接在主仓库分支开发;本文的边界规则(§2-§4)在收编后继续作为模块边界约束,不随 fork 退役。
