# 成果首次核实规约对账

预期从双创需求 PR-06/08/11/12、OP-01、AC-01 和成果核实设计推导，不把原科研 Task 上传/奖励规则套用于成果。

| # | 规则引用 | 预期行为 | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| R1 | 材料检查/G4/G5 | 明确完整扫描 OK 才通过；错误、异常、缺病毒库、命令不可用、超时与超限拒绝 | `tests/unit/integrations/test_evidence_scanner.py::test_complete_stream_and_explicit_clean_response` + `tests/unit/integrations/test_evidence_scanner.py::test_protocol_failure_never_becomes_success` + `tests/unit/integrations/test_evidence_scanner.py::test_detected_threat_is_never_clean` + `tests/unit/integrations/test_evidence_scanner.py::test_scan_deadline_and_socket_failure_are_unavailable` | 已覆盖（针对性真实 TCP 单测；非全量门禁） |
| R2 | 材料类型与截断 | 不相信声明 MIME，拒绝不支持类型、签名不符、明显截断；不声称完整 PDF/图片解码 | `tests/unit/integrations/test_evidence_scanner.py::test_basic_signature_and_structure_match_declared_type` + `tests/unit/integrations/test_evidence_scanner.py::test_disguised_unsupported_and_truncated_content_rejected` | 已覆盖（基本类型/结构识别） |
| R3 | G1/G2：真实扫描 | 官方病毒库、真实 Clamd，正常文件通过、标准 EICAR 拒绝；10 MiB 容器内末尾独立 EICAR 被检测 | `tests/integration/test_evidence_scanner_smoke.py::test_real_clamd_ready_and_clean_file` + `tests/integration/test_evidence_scanner_smoke.py::test_real_clamd_detects_eicar` + `tests/integration/test_evidence_scanner_smoke.py::test_real_clamd_scans_entire_10_mib_including_final_threat` | 已覆盖（CQ_EVIDENCE_SCAN_SMOKE=1 本地真实服务；完整应用 wiring 尚见 G-1） |
| R4 | PR-06/OP-01：证明权限 | 负责人上传/授权领取人读；其他账号和未领取 ADMIN 不读；撤权和审计失败拒绝 | `tests/integration/innovation/test_achievement_review_operations.py::test_operator_claim_gates_private_material_and_publication` + `tests/integration/innovation/test_review_races.py::test_read_rechecks_revocation_after_external_io` + `tests/integration/innovation/test_achievement_review_operations.py::test_failed_snapshot_or_file_audit_never_returns_private_data` | 已覆盖（后端，前端见 G-4） |
| R5 | PR-08/11：不可变首次核实 | 首次提交冻结快照；待审先撤回再改；旧审不得公开新稿；批准/撤回竞争只能一方成功 | `tests/integration/innovation/test_achievement_workflow.py::test_submit_freezes_saved_content_and_withdraw_allows_edit` + `tests/integration/innovation/test_review_races.py::test_approval_and_withdrawal_have_one_committed_winner` | 已覆盖（真实独立 PG 连接，两个赢家顺序） |
| R6 | PR-12/AC-01 | 通过后显式发布更新免复审；保存不公开；匿名/未审/下架与私有字段不公开；更新不恢复下架 | `tests/integration/innovation/test_public_achievements.py::test_public_projection_visibility_and_private_fields` + `tests/integration/innovation/test_achievement_workflow.py::test_publish_update_is_explicit_immutable_and_never_restores_takedown` | 已覆盖（真实 HTTP/PG，前端见 G-4） |
| R7 | UI/G19：真实闭环 | 三账号浏览器闭环、手机键盘、axe、固定字体视觉及生成契约工件随功能 | `tests/../../frontend/e2e/innovation-review.spec.ts` + `tests/../../frontend/e2e/innovation-review-visual.spec.ts` + `tests/../../frontend/src/__tests__/innovation-review.test.ts` | 已覆盖（功能闭环及三张新PNG；完整门禁与最终独立评审仍待本轮结果） |
| R8 | G1/G2：材料组合 | 真实生产 provider 通过 PG/MinIO/ClamAV 完成；写一次与类型/长度签名生效，读取上限生效 | `tests/integration/innovation/test_evidence_composition_smoke.py::test_real_provider_checks_uploaded_bytes_and_write_once` + `tests/integration/innovation/test_evidence_composition_smoke.py::test_evidence_signature_pins_type_and_size` | 已覆盖（双 smoke 标记，本地真实组合） |
| R9 | 有界检查/锁后重验 | 扫描不持业务锁；移除、停用或新 attempt 完成后，旧扫描结果不得写入 READY | `tests/integration/innovation/test_evidence_races.py::test_late_scan_revalidates_committed_state` | 已覆盖（真实 PG 独立提交连接；可控外部扫描 fake） |
| R10 | 本人材料生命周期/G12 | 资格、归属、大小数量、缺失、过期、扫描失败重试、已检内容变化和审计失败均执行拒绝规则 | `tests/integration/innovation/test_achievement_evidence.py::test_real_http_upload_check_and_private_proxy` + `tests/integration/innovation/test_achievement_evidence.py::test_owner_qualification_scope_and_authentication` + `tests/integration/innovation/test_achievement_evidence.py::test_finish_audit_failure_never_commits_ready` | 已覆盖（负责人及运营接口均已覆盖） |
| R11 | PR-08/11：本人提交版本 | 已保存概况/身份/说明/READY 材料才可提交；冻结版本，待审先撤回再改；旧撤回不关闭新单；历史不可写 | `tests/integration/innovation/test_achievement_workflow.py::test_submit_freezes_saved_content_and_withdraw_allows_edit` + `tests/integration/innovation/test_achievement_workflow.py::test_new_submission_uses_new_revision_and_old_withdrawal_cannot_close_it` + `tests/integration/innovation/test_achievement_workflow.py::test_database_rejects_revision_and_checked_content_mutation` | 已覆盖（并包含独立 PG 连接的运营批准/撤回竞争） |
| R12 | PR-12：显式更新 | 保存草稿不改公开指针，发布更新不创建二次审单、不改首次核实日期、不恢复下架 | `tests/integration/innovation/test_achievement_workflow.py::test_publish_update_is_explicit_immutable_and_never_restores_takedown` | 已覆盖（真实运营批准及公开响应由 Task 4/5 覆盖） |
| R13 | 异常恢复/G12 | 上传失败保留原文件请求，可显式放弃本地重试并重新选择；移除服务器记录不将页面永久锁死 | `tests/../../frontend/e2e/innovation-review.spec.ts` | 已覆盖（真实签名PUT403→移除意向→明确放弃→重新上传，RED缺入口→GREEN闭环1通过59.4秒） |

## 缺口明细

- G-1：已关闭后端材料与运营读取缺口（R4/R8–R10）。
- G-2：已关闭后端工作流与运营批准/撤回竞争缺口（R5/R11）。
- G-3：已关闭后端公开白名单及更新/下架隔离缺口（R6/R12）。
- G-4：页面闭环与后端全量门禁已通过；前端最终电池、视觉复验及独立评审仍在Task5收尾，不能声称完整三库demo。

## 执行记录

2026-10-09 Task 1：针对性 27 TCP/类型单测 + 3 真实扫描烟测共 30 通过。首次 10 MiB 测试失败原因是把 EICAR 当作可任意拼接的标记；独立测试显示 68 字节文件被识别、加 100 字节前缀不被识别、含独立 EICAR 的 stored ZIP 被识别。改用确切 10 MiB stored ZIP 验证完整文件/嵌套检查，不改变 API 类型白名单或扫描通过规则。全部原始输出在工作区 `.local-dev/logs/review-scanner-*.log`。

2026-10-09 Task 2：新负责人生命周期、独立连接并发、真实组合与既有存储回归共 56 项通过（78.00 秒、出口 0）。先记录缺失模块 RED，再实现；测试 harness 的 FrozenClock 不可变、显式 fixture 导入问题已修正。Ruff 格式/检查出口 0，mypy 六个源文件出口 0；独占库 0030 升级及退回 0029 后再次升级出口 0。完整输出 `.local-dev/logs/review-evidence-regression.log`，不表示 Task 3–5 或全项目门禁完成。

2026-10-09 Task3：35 项工作流/材料/草稿/独立连接回归通过（21.13 秒、出口0）；源码26文件 mypy、30文件Ruff、0031往返迁移与 Alembic 模型对账出口0。日志 `.local-dev/logs/review-workflow-regression.log`；原始缺失行为 RED 和测试夹具错误日志保留，不降低数据库约束适应错误夹具。运营审核与前端闭环尚未实现。

2026-10-09 Task4：144 项运营/公开投影/独立连接竞争及通知回归通过（19.35 秒、出口0）。含批准与撤回两种先后顺序、领取排他、文件外部读取期间撤权、审计及通知持久化失败的回滚。mypy32源文件、Ruff13文件、0032往返迁移及模型对账出口0。日志 `.local-dev/logs/review-operations-regression.log`；前端及全量门禁仍见G-4。

2026-10-09 Task5 补充：真实三账号闭环通过，包含一次真实提交后响应503、一次真实决定后网络响应丢失，重试体与原请求完全相同；同浏览器跨标签切换账号后，旧私密审核快照立即消失。已保留失效回归 RED→GREEN；未知错误安全文案保留请求 ID。历史证明不占新上传五槽位的回归已通过。`test_review_boundaries.py` 增加真实PG/HTTP的领取重放、回避释放、资格变化、私密文件大小/类型/摘要变化、上传移除和活动检查租约等边界。Linux双创子集188项通过、98.6%为诊断子集，不能替代最终完整棘轮。
