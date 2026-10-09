# 成果核实与校内展示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. 用户已明确要求回顾两份材料后继续；采用本会话逐项执行，最终独立复核，不重复请求执行批准。

**Goal:** 负责人上传安全证明并提交不可变成果，运营核实后校内可见，已公开成果更新免复审。

**Architecture:** 沿用 innovation 域、SQLAlchemy/PostgreSQL、Clock、审计及通知端口。证明通过独立存储/扫描端口；网络 I/O 不持业务锁。审核只处理不可变版本，公开 DTO 不包含私有材料或负责人四项资料。

**Tech Stack:** FastAPI/Pydantic、PostgreSQL/Alembic、S3/MinIO、隔离 ClamAV、Next.js/TypeScript、pytest/Playwright。

**Spec:** `docs/superpowers/specs/2026-10-09-innovation-achievement-review-design.md`；业务来源为工作区双创需求 v0.7 PR-06/08/11/12、OP-01、AC-01。

## Global Constraints

- 资格与成果核实分离；运营仍为 STUDENT，不能创建 Task 或任意调积分；校内认证接入仍为正式上线前待对接项。
- PDF/PNG/JPEG，单份 10 MiB、提交最多 5 份；只有完成类型核对与病毒扫描才可 READY，超限、跳过、服务失败不放行。
- 使用现有版本 CAS、真实 PG 锁及审计；按用户 UUID→授权→项目→成果→审核单/材料加锁；业务锁内不做远程存储或扫描。
- 待审修改先撤回；已通过同一成果更新免复审，新成果仍首次审核；下架与首次核实分开，更新不恢复下架。
- 材料仅当前负责人及该单领取人可读；领取人撤权/停用立即失去权限；公开白名单不含学号、证据、对象 key。
- 通知 durable、唯一事件键；不改变任务域格式/奖励规则、不降低已有覆盖地板或放宽像素阈值。
- 本计划执行记录保留在独立 ledger；既有开发数据不清理，测试使用独占 `campusquest_test_ie_review_20261009`。

## Review Focus

1. 上传 URL 重放与对象替换：第二次 PUT 必须 412，检查后的文件与审核引用 SHA-256 一致（Task 2 真实 MinIO 测试）。
2. 扫描器实际跳过/协议异常/不可用：不能误记 READY，未知响应拒绝，30 秒总预算，10 MiB 边界（Task 1 协议及真实烟测）。
3. 操作等锁时账号/运营授权被撤销：锁后重新校验，不返回敏感材料（Task 3/4 两连接 PG 测试）。
4. 撤回与审核竞争、旧版本和幂等键参数变化：单一成功结果、旧审不公开新稿、同键不同载荷 409（Task 3/4）。
5. 浏览者通过直链、下架后更新、前端换账号保留旧数据：不可读证明/未审成果，不恢复下架，清除前账号请求结果（Task 4/5）。

### Task 1: 文件检查端口与真实 ClamAV 组合

**Files:** Create `backend/app/integrations/evidence_scanner.py`, `backend/app/integrations/evidence_scanner_clamd.py`, `backend/tests/unit/integrations/test_evidence_scanner.py`, `backend/tests/integration/test_evidence_scanner_smoke.py`, `infra/clamav/clamd.conf`, `infra/clamav/freshclam.conf`, `infra/docker-compose.clamav.yml`; modify settings/env documentation.

**Interfaces:** Produces `EvidenceScanner.scan(content: bytes) -> None`（威胁/无效内容/检查不可用分别异常）及 `ClamdEvidenceScanner.check() -> bool`；不接受客户端路径。

- [x] 写真实 TCP 协议测试：完整 INSTREAM 分块及结束标记；明确 OK 才通过；FOUND、ERROR、无终止响应、超长响应、断连、超时、缺少病毒库/命令均拒绝；类型伪装和截断拒绝。生产改动误将任一错误当 OK 必须使测试失败。
- [x] 运行新测试，确认新端口缺失/行为缺失失败；再实现有总截止时间和字节/响应上限的端口与 adapter。
- [x] 固定扫描配置、内部/回环 TCP，官方病毒库更新；真实烟测健康文件与 EICAR、10 MiB 及检查器停用场景，不拿测试自定义病毒库替代官方库。
- [x] 运行 `python -m pytest tests/unit/integrations/test_evidence_scanner.py -q`（预期全 PASS）、Ruff/mypy；真实烟测按 opt-in 与完整输出记录；提交。

### Task 2: 成果证明意向、完成检查与受控读取

**Files:** Create innovation `evidence_models.py`, `evidence_schemas.py`, `evidence_service.py`, `evidence_router.py`, migration `0030_innovation_evidence.py`, integration `innovation/test_achievement_evidence.py`; modify object-storage port/real adapter/test fake, Base registry and main composition.

**Interfaces:** Consumes Task 1 scanner。Storage produces `create_evidence_upload_url(achievement_id, content_type, content_length, expires_in) -> UploadUrl` and `read_bounded_object(object_key, max_bytes) -> StoredObject`。`EvidenceService` exposes owner `create_intent`, `list_owned`, `complete`, `remove`, `read_content`，均接收 db、actor、project/achievement/evidence IDs 和 AuditContext。

- [x] 先写意向与完成失败用例：未开通资格、跨账号/项目、类型/数量/大小、过期、实际大小/类型不符、文件缺失、扫描失败/重试、审计失败均不能 READY/返回文件。
- [x] 运行真实 PG 测试观察缺失行为失败，再实现 PENDING/CHECKING/READY/REJECTED、检查 attempt CAS、SHA-256、脱敏审计与受控读取；网络前提交短事务，返回前重锁验证资格/归属/状态。
- [x] 路由挂载既有成果子路径 `/evidence`，签名字段仅意向接口回传；下载强制 attachment/nosniff/private-no-store，不回传 key/下载签名 URL。
- [x] 从空独占库迁移到 head；真实 MinIO 写一次/大小 pin/类型 pin/受限读取；运行新增集成测试（预期全 PASS）及静态检查；提交。

### Task 3: 不可变版本、提交/撤回/公开更新

**Files:** Create innovation `review_models.py`, `review_schemas.py`, `review_service.py`, `review_router.py`, migration `0031_innovation_achievement_review.py`, integration `innovation/test_achievement_workflow.py`; modify draft editing service, Base registry。

**Interfaces:** Produces `AchievementReviewService.workflow`, `submit`, `withdraw`, `publish_update`，输入 Actor、父子 IDs、已保存版本、request_id、材料 IDs；响应 workflow 版本和审单 ID，不公开敏感快照。Task 2 的材料增加审核引用冻结检查。

- [x] 测试先行：四项负责人/五项项目概况/成果描述/至少一份 READY 材料；提交幂等及同键不同内容 409；每份成果首次核实；待审禁止修改/删除材料；撤回重交旧单无效。
- [x] 实现 workflow/revision/review_case/材料引用外键及唯一、状态组合约束，提交保存项目/成果/身份的不可变快照。PG 两连接验证撤回竞争和旧版本不替换。
- [x] 已通过“保存草稿”不公开；显式 publish-update 原子切换版本而不建审核单、不改 first_approved_at；下架后更新不恢复。
- [x] 运行 `python -m pytest tests/integration/innovation/test_achievement_workflow.py -m integration -q`（预期全 PASS），既有成果草稿回归、静态与迁移验证；提交。

### Task 4: 运营领取/审核、公开投影及 durable 通知

**Files:** Create innovation `review_operations_service.py`, `review_operations_router.py`, `public_achievement_service.py`, `public_achievement_router.py`, integration `innovation/test_achievement_review_operations.py`, `innovation/test_public_achievements.py`; modify notifications enum/模板/路由映射及 main。

**Interfaces:** Ops `/ie/ops/achievement-reviews` list/claim/detail/decision；public `/ie/achievements` list/detail。领取与决定携带 expected_version，决定还携带 revision_id/request_id/reason；返回显式响应 schema。

- [x] 先测试无授权/本人项目回避、领取冲突、领取后才能查看快照、撤权/停用阻断、未领取 ADMIN 无材料读取；审计失败不暴露内容。
- [x] 实现用户顺序锁和锁后身份重验，返回理由必填，旧版本/撤回竞争 409；通过设置公开版本、首次核实时间，状态与审计/通知意图同事务。
- [x] 公开分页仅 APPROVED/NORMAL/public_revision 白名单，匿名/停用/未审/下架直链拒绝；后续更新明确标记未逐项复审。
- [x] 测试通知唯一、重复决定无二次通知/公开，真实 PG 两连接批准与撤回竞争；运行新增及通知回归（预期全 PASS）/静态；提交。

### Task 5: 本人、运营与校内浏览界面及最终门禁

**Files:** Extend existing frontend innovation achievement editor/API client; add review workspace and public list/detail pages、pure tests/e2e/axe/固定字体 Linux 基线；OpenAPI/generated types/规约矩阵/选择器/豁免工件随代码提交。

**Interfaces:** 使用上述生成契约与现有 fetch/会话隔离，不自创角色和本地 fake 成功；文件上传把 adapter 签名 headers 原样传递、Blob size 与 pin 一致。

- [ ] 先写保存/提交/撤回/退回重交/免复审更新、文件错误状态及账户切换的失败用例；实现并跑前端纯测和静态。
- [ ] 负责人、运营、普通浏览者演示账号跑真实浏览器闭环；证明普通账号不能下载证明，退回原因可见，保存不公开，更新不复审；手机、键盘、axe。
- [ ] 同 HEAD 更新 OpenAPI、生成类型、矩阵及工件，跑完整相关双树门禁和权威 Linux 后端覆盖率（greenlet/thread）与视觉；所有失败如实列名，不以旧日志补数。
- [ ] 对本计划完整差异作一次独立评审；重要问题以失败回归修复；保留 Draft PR，不合并。记录具体用户验收路径与尚未实现三库范围，提交并推送。
