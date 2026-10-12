# 人才库规约测试矩阵

预期先从 R4 spec §1–8 推导，2026-10-12 落笔。A01 对应 R4-A01，其余同序。
Task 1/2 的真实 PG/HTTP 与存储扫描验证见工作区 `.local-dev/logs/r4-task1-*`、`r4-task2-*`；公共接口、前端与最终验收尚未完成。

| 编号 | 规约预期 | 验证位置 | 状态 |
| --- | --- | --- | --- |
| A01 | 普通学生无负责人资格，无经历/文件/联系也可开启；默认隐藏 | `tests/integration/innovation/test_talent_lifecycle.py::test_new_student_can_publish_without_work_or_contact` | 已覆盖 |
| A02 | 保存不改公开版；旧发布重放不恢复关闭 | `tests/integration/innovation/test_talent_lifecycle.py::test_saved_changes_remain_private_and_replay_cannot_reopen` | 已覆盖 |
| A03 | 暂不接洽与开关分离，仍能公开；公共展示待 Task 3 | `tests/integration/innovation/test_talent_lifecycle.py::test_last_contact_can_be_revoked_without_hiding_talent`；G-1 | 缺口 |
| A04 | 标签/建议私有且去重，字面关键词、多标签全部匹配/分页 | `tests/integration/innovation/test_talent_lifecycle.py::test_tag_suggestions_are_private_deduplicated_requests`；G-1 | 缺口 |
| A05 | PDF/PNG/JPEG、10MiB/10库存/5公开，整批替换可用 | `tests/unit/innovation/test_talent_file_schemas.py`；`tests/integration/innovation/test_talent_files.py::test_five_public_files_can_all_be_replaced_before_closing_old_version`；`tests/integration/innovation/test_talent_file_races.py::test_concurrent_upload_intents_cannot_exceed_ten_records` | 已覆盖 |
| A06 | 显式授权文件才可读，关闭/撤回旧链接拒绝 | `tests/integration/innovation/test_talent_files.py::test_public_file_requires_revocation_and_cannot_be_published_again`；G-1 | 缺口 |
| A07 | 身份/治理限制不可绕过；列表搜索直链同样生效 | `tests/integration/innovation/test_talent_lifecycle.py::test_moderation_cannot_be_cleared_by_saving_or_publishing`；`tests/integration/innovation/test_talent_races.py::test_account_disabled_before_publication`；G-1 | 缺口 |
| A08 | 普通详情无联系值，点击按版本读取；限流/审计故障不给值 | G-1 | 缺口 |
| A09 | 幂等、版本冲突、PG并发最多一次改变；审计失败原子回滚，历史不可改 | `tests/integration/innovation/test_talent_races.py`；`tests/integration/innovation/test_talent_lifecycle.py::test_failed_audit_rolls_back_publication`；`tests/integration/innovation/test_talent_lifecycle.py::test_public_history_is_immutable_in_postgres`；`tests/integration/innovation/test_talent_lifecycle.py::test_public_pointer_cannot_use_another_students_revision` | 已覆盖 |
| A10 | 外部I/O无持锁，迟到扫描不复活移除文件；公共读取待 Task 3 | `tests/integration/innovation/test_talent_file_races.py`；G-1 | 缺口 |
| A11 | 就地纠错/键盘/恢复，迟到回执不覆盖新输入，账号切换不泄露 | G-3 | 缺口 |
| A12 | 成果、招募、原任务不回归；模拟资料标记；学校认证仍未接 | G-4 | 缺口 |

## 缺口明细

- G-1：Task 3 公共查询、联系、文件最终授权待实施和验证。
- Task 2 文件 lifecycle、审计回滚、私有读权限与扫描竞态已验证；真实 adapter 和生产组合分别见 `tests/integration/test_object_storage_smoke.py::test_talent_file_private_namespace_and_signed_write_once_contract`、`tests/integration/innovation/test_talent_composition_smoke.py`，均启用 opt-in 服务。
- G-3：Task 4 前端和 E2E 待实施和验证。
- G-4：Task 5 全量门禁与用户视角走查待完成。
