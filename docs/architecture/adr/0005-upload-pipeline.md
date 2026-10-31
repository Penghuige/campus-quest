# ADR 0005: 上传流水线——预签名直传、单次 intent、§32 幂等与异步校验

日期: 2026-10-31 · 状态: 已接受
关联: 产品 spec §10/§10.1/§11/§12/§32 ·
backend/app/modules/submissions/upload_service.py ·
backend/app/workers/jobs/validate_submission.py ·
docs/loadtest/capacity-report-2026-10.md（S1 实证）

## 决策

大文件（上限 200 MB）不穿过应用进程，正确性与吞吐靠四件事：

1. **三步流，API 永不碰文件字节**：`POST /submissions/upload-intent`
   （校验领取/类型/大小/窗口 → 发预签名 PUT 与 pinned 字节数）→
   浏览器直传对象存储 → `POST /submissions/upload-complete`
   （HEAD 验对象 → 建 Submission → 派发异步校验）。对象 key 服务端生成，
   永不来自客户端文件名（spec §40：key 不入公共 DTO）。
2. **intent 是单次授权**：`finalized_submission_id` 从 NULL→id 恰好一次，
   consumed_at 同事务设置；过期/烧毁的 intent 有明确的拒绝语义。
3. **§32 幂等锚在库上**：重放已完成的 intent 返回**同一个** Submission
   （`UNIQUE(claim_id, version)` + intent 的幂等指针列），客户端重试
   永不产生重复提交——容量战役的 S1 检查器在负载后按此断言零重复。
4. **校验异步且沙箱化**：Celery worker 在子进程里做内容真实检测
   （声明类型 vs 检测类型，§12 的 FILE_TYPE_NOT_ALLOWED 家族），墙钟
   超时/崩溃/OOM 都到达终态报告（VALIDATION_TIMED_OUT 等），worker
   永不悬挂；失败回边把领取恢复到可行动状态，学生的第一次坏文件
   不烧掉领取。

## 理由

- 直传把带宽从应用进程卸载给对象存储——API 进程只做授权与元数据，
  这是单机能扛"截止前全班同时交大文件"（S1 场景）的结构性前提。
- 单次 intent + 库上幂等让"恰好一次提交"不依赖网络的重试语义——
  网络越糟重试越多，库上幂等让重试免费。
- 两种文件族（结构化 CSV/XLSX/SQLite vs 文档 DOCX/PDF，spec §10.1）
  一条流水线两种策略：结构化按 schema 机器校验，文档做完整性检查 +
  人工审阅承载内容判断。

## 被否决的替代

- **应用进程中转上传**：200MB × 并发穿过 uvicorn，内存与带宽双杀；
  S1 的崩溃点会从"磁盘先倒"变成"进程先倒"。
- **上传即校验（同步）**：客户端等待解析大表；坏文件烧掉请求超时。

## 后果

- 上传正确性依赖"预签名 URL 只写一次"（If-None-Match 语义），对象
  存储的该行为是硬前提，适配器层固化。
- 校验状态机（UPLOADED→VALIDATING→终态 + 领取的 VALIDATING 中间态与
  失败回边）是模块内最复杂的部分，其测试密度与之相称
  （tests/integration/submissions/test_validation_worker.py 全家）。
