# 成果首次核实规约对账

预期从双创需求 PR-06/08/11/12、OP-01、AC-01 和成果核实设计推导，不把原科研 Task 上传/奖励规则套用于成果。

| # | 规则引用 | 预期行为 | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| R1 | 材料检查/G4/G5 | 明确完整扫描 OK 才通过；错误、异常、缺病毒库、命令不可用、超时与超限拒绝 | `tests/unit/integrations/test_evidence_scanner.py::test_complete_stream_and_explicit_clean_response` + `tests/unit/integrations/test_evidence_scanner.py::test_protocol_failure_never_becomes_success` + `tests/unit/integrations/test_evidence_scanner.py::test_detected_threat_is_never_clean` + `tests/unit/integrations/test_evidence_scanner.py::test_scan_deadline_and_socket_failure_are_unavailable` | 已覆盖（针对性真实 TCP 单测；非全量门禁） |
| R2 | 材料类型与截断 | 不相信声明 MIME，拒绝不支持类型、签名不符、明显截断；不声称完整 PDF/图片解码 | `tests/unit/integrations/test_evidence_scanner.py::test_basic_signature_and_structure_match_declared_type` + `tests/unit/integrations/test_evidence_scanner.py::test_disguised_unsupported_and_truncated_content_rejected` | 已覆盖（基本类型/结构识别） |
| R3 | G1/G2：真实扫描 | 官方病毒库、真实 Clamd，正常文件通过、标准 EICAR 拒绝；10 MiB 容器内末尾独立 EICAR 被检测 | `tests/integration/test_evidence_scanner_smoke.py::test_real_clamd_ready_and_clean_file` + `tests/integration/test_evidence_scanner_smoke.py::test_real_clamd_detects_eicar` + `tests/integration/test_evidence_scanner_smoke.py::test_real_clamd_scans_entire_10_mib_including_final_threat` | 已覆盖（CQ_EVIDENCE_SCAN_SMOKE=1 本地真实服务；完整应用 wiring 尚见 G-1） |
| R4 | PR-06/OP-01：证明权限 | 负责人上传/授权领取人读；其他账号和未领取 ADMIN 不读；撤权和审计失败拒绝 | — | 缺口 G-1（Task 2/4 未实现） |
| R5 | PR-08/11：不可变首次核实 | 首次提交冻结快照；待审先撤回再改；旧审不得公开新稿；批准/撤回竞争只能一方成功 | — | 缺口 G-2（Task 3/4 未实现） |
| R6 | PR-12/AC-01 | 通过后显式发布更新免复审；保存不公开；匿名/未审/下架与私有字段不公开；更新不恢复下架 | — | 缺口 G-3（Task 3/4 未实现） |
| R7 | UI/G19：真实闭环 | 三账号浏览器闭环、手机键盘、axe、固定字体视觉及生成契约工件随功能 | — | 缺口 G-4（Task 5 未实现） |

## 缺口明细

- G-1：材料生命周期、存储组合和读取权限待 Task 2；运营领取后的材料读取待 Task 4。
- G-2：workflow/revision/review_case 与竞争测试待 Task 3/4。
- G-3：公开白名单及更新/下架隔离待 Task 3/4。
- G-4：页面及跨树完整门禁待 Task 5；现阶段不能声称完成成果核实或三库 demo。

## 执行记录

2026-10-09 Task 1：针对性 27 TCP/类型单测 + 3 真实扫描烟测共 30 通过。首次 10 MiB 测试失败原因是把 EICAR 当作可任意拼接的标记；独立测试显示 68 字节文件被识别、加 100 字节前缀不被识别、含独立 EICAR 的 stored ZIP 被识别。改用确切 10 MiB stored ZIP 验证完整文件/嵌套检查，不改变 API 类型白名单或扫描通过规则。全部原始输出在工作区 `.local-dev/logs/review-scanner-*.log`。
