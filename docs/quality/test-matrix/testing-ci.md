# testing-ci 规约-测试对账矩阵

> 建立日期：2026-10-26 · 域：测试/CI 基建自身（架构图 freshness 门禁 + 测试报告 artifact provenance；docs/best-practices/testing-ci.md 条目 2/3 的实现面）
> 方法论：`campusquest-test-process` skill；**预期列先于测试检索落笔**；缺口列出不补；不匹配上报不擅改。
> 规约来源：docs/best-practices/testing-ci.md（三源沉淀，owner 批准）+ docs/operations/test-report.md §2.1；架构图 freshness 的机制规约在 scripts/generate_as_built.py 模块 docstring（派生图+freshness 门）。
> 域界：业务域行为不建行（各业务矩阵已有）；本表只对账基建自身契约。

> 覆盖章节：docs/best-practices/testing-ci.md

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 4 | TC1-TC4 |
| 缺口 | 0 | — |
| 不匹配 | 0 | — |

## 矩阵

| 行 | 规约来源 | 预期 | 测试 | 状态 |
|---|---|---|---|---|
| TC1 | 实践条目 2（报告留存）之 freshness 机制 | as-built 派生图 freshness 门：`--check` 对**内容**敏感（真实树漂移=stale 红，基线实测 exit 1→再生成 exit 0）；对信息性 **HEAD 戳钝感**（逐字节比较会让每次提交即假红——门禁不可用）；CLI 端到端两向（仅戳差=0/漂移行=1） | `tests/unit/scripts/test_generate_as_built.py::test_check_freshness_ignores_the_head_stamp` / `::test_check_freshness_fails_on_real_drift` / `::test_check_cli_passes_on_stamp_only_difference` | 已覆盖 |
| TC2 | 实践条目 3（来源可辨识）stamp 契约 | `--stamp-artifact` 原样落盘六要素：`schema=1`、全长 `source_sha`、`dirty` 布尔、文件内容 `artifact_sha256`、产生命令、`generated_at_utc`（UTC `%Y-%m-%dT%H:%M:%SZ`）；sidecar 紧邻工件同名 | `tests/unit/scripts/test_test_report_provenance.py::test_stamp_writes_the_sidecar` | 已覆盖 |
| TC3 | 实践条目 3 判定表（fail-closed） | 分类按内容不按宽容：完整有效 sidecar→fresh/同 SHA+dirty→dirty（不称 commit fresh）/跨 SHA→stale；**元数据不完整或非法一律 unknown**——缺 sidecar（旧格式兼容）、坏 JSON、缺/未知 `schema`、缺/非字符串/非 UTC 形态 `generated_at_utc`、空 `source_sha`（≠stale）、stamp 后文件被改（哈希不符）、无比较基（HEAD 不可知）；加严不得反向放宽 fresh（有效 sidecar 的 stale 判定保持） | `tests/unit/scripts/test_test_report_provenance.py`（21 腿：`test_matching_sha_clean_and_intact_is_fresh` / `test_same_sha_but_dirty_is_dirty_not_fresh` / `test_cross_sha_is_stale` / `test_missing_sidecar_is_unknown` / `test_corrupt_sidecar_is_unknown` / `test_edited_after_stamping_is_unknown` / `test_unknown_head_classifies_unknown` / `test_missing_schema_is_unknown` / `test_unknown_schema_version_is_unknown` / `test_missing_generated_at_is_unknown` / `test_non_string_generated_at_is_unknown` / `test_non_utc_generated_at_is_unknown` / `test_empty_source_sha_is_unknown` / `test_valid_schema_and_utc_still_classify_by_content`） | 已覆盖 |
| TC4 | 实践条目 3 报告集成 | 每个**存在**工件的层携带来源章（playwright.json 双层共享同一来源）；页首摘要按态计数（混合来源可见不合并）；缺结果文件的层=未运行，**不产生**来源判定（≠unknown）；历史 meta 保留逐层摘要 | `tests/unit/scripts/test_test_report_provenance.py::test_layers_carry_their_provenance_and_the_summary_names_mixed` / `::test_missing_layers_have_no_provenance_not_unknown` / `::test_shared_artifact_feeds_both_playwright_layers` | 已覆盖 |

## 缺口明细

无。

## 不匹配明细

无。
