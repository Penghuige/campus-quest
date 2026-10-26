# 人才库规约测试矩阵
> 覆盖章节：docs/superpowers/specs/2026-10-12-innovation-talent-design.md#§1 · §2 · §3 · §4 · §5 · §6 · §7 · §8

预期先从 R4 spec §1–8 推导，2026-10-12 落笔。A01 对应 R4-A01，其余同序。
Tasks 1–3 的真实 PG/HTTP 与存储扫描验证见工作区 `.local-dev/logs/r4-task1-*`、`r4-task2-*`、`r4-task3-*`；最终13个人才浏览器场景、手机长内容及新旧视觉均通过；全量门禁与当前产品输入对应见 `docs/quality/innovation-talent-verification.md`，不是历史结果替代。

| 编号 | 规约预期 | 验证位置 | 状态 |
| --- | --- | --- | --- |
| A01 | 普通学生无负责人资格，无经历/文件/联系也可开启；默认隐藏 | `tests/integration/innovation/test_talent_lifecycle.py::test_new_student_can_publish_without_work_or_contact` | 已覆盖 |
| A02 | 保存不改公开版；旧发布重放不恢复关闭 | `tests/integration/innovation/test_talent_lifecycle.py::test_saved_changes_remain_private_and_replay_cannot_reopen` | 已覆盖 |
| A03 | 暂不接洽与开关分离，仍能公开 | `tests/integration/innovation/test_public_talents.py::test_literal_search_all_tags_and_cooperation_filters`；`tests/integration/innovation/test_talent_contacts.py::test_revoking_last_contact_keeps_profile_visible_and_empty_authorization` | 已覆盖 |
| A04 | 标签/建议私有且去重，字面关键词、多标签全部匹配/分页 | `tests/integration/innovation/test_talent_lifecycle.py::test_tag_suggestions_are_private_deduplicated_requests`；`tests/integration/innovation/test_public_talents.py::test_literal_search_all_tags_and_cooperation_filters`；`tests/integration/innovation/test_public_talents.py::test_pagination_ties_are_stable_and_tag_filters_are_bounded` | 已覆盖 |
| A05 | PDF/PNG/JPEG、10MiB/10库存/5公开，整批替换可用 | `tests/unit/innovation/test_talent_file_schemas.py`；`tests/integration/innovation/test_talent_files.py::test_five_public_files_can_all_be_replaced_before_closing_old_version`；`tests/integration/innovation/test_talent_file_races.py::test_concurrent_upload_intents_cannot_exceed_ten_records` | 已覆盖 |
| A06 | 显式授权文件才可读，关闭/撤回旧链接拒绝 | `tests/integration/innovation/test_talent_files.py::test_public_file_requires_revocation_and_cannot_be_published_again`；`tests/integration/innovation/test_public_talents.py::test_contacts_and_files_require_current_version_and_close_stops_access`；`tests/integration/innovation/test_talent_public_file_races.py` | 已覆盖 |
| A07 | 身份/治理限制不可绕过；列表搜索直链同样生效 | `tests/integration/innovation/test_talent_lifecycle.py::test_moderation_cannot_be_cleared_by_saving_or_publishing`；`tests/integration/innovation/test_talent_races.py::test_account_disabled_before_publication`；`tests/integration/innovation/test_public_talents.py::test_current_identity_and_governance_apply_to_list_and_detail` | 已覆盖 |
| A08 | 普通详情无联系值，点击按版本读取；限流/审计故障不给值 | `tests/integration/innovation/test_public_talents.py::test_public_projection_excludes_private_sentinels`；`tests/integration/innovation/test_talent_contacts.py`；`tests/integration/innovation/test_talent_public_file_races.py::test_public_file_audit_failure_never_returns_loaded_bytes` | 已覆盖 |
| A09 | 幂等、版本冲突、PG并发最多一次改变；审计失败原子回滚，历史不可改 | `tests/integration/innovation/test_talent_races.py`；`tests/integration/innovation/test_talent_lifecycle.py::test_failed_audit_rolls_back_publication`；`tests/integration/innovation/test_talent_lifecycle.py::test_public_history_is_immutable_in_postgres`；`tests/integration/innovation/test_talent_lifecycle.py::test_public_pointer_cannot_use_another_students_revision` | 已覆盖 |
| A10 | 外部I/O无持锁，迟到扫描不复活移除文件；关闭/撤权拒绝已读入字节 | `tests/integration/innovation/test_talent_file_races.py`；`tests/integration/innovation/test_talent_public_file_races.py` | 已覆盖 |
| A11 | 就地纠错/键盘/恢复，迟到回执不覆盖新输入，账号切换不泄露 | `frontend/src/__tests__/talent-form.test.ts`；`frontend/src/__tests__/talent-recovery.test.ts`；`frontend/src/__tests__/talent-presentation.test.ts`；`frontend/e2e/innovation-talents.spec.ts`；`frontend/e2e/innovation-talents-visual.spec.ts` | 已覆盖 |
| A12 | 成果、招募、原任务不回归；模拟资料标记；学校认证仍未接 | `frontend/e2e/innovation-talents.spec.ts`（模拟资料与认证文案断言）；`frontend/e2e/visual-regression.spec.ts`（原任务界面回归，标准电池）；`docs/quality/innovation-talent-verification.md` | 已覆盖（批 1 视角：模拟资料、原任务回归、认证文案；成果/招募回归行归批 2/4 各自矩阵） |

## 对账说明与明确边界

- Task 3 公共查询、主动联系、最终文件授权已覆盖；真实生产组合含观察者下载与关闭后旧请求拒绝。
- Task 2 文件 lifecycle、审计回滚、私有读权限与扫描竞态已验证；真实 adapter 和生产组合分别见 `tests/integration/test_object_storage_smoke.py::test_talent_file_private_namespace_and_signed_write_once_contract`、`tests/integration/innovation/test_talent_composition_smoke.py`，均启用 opt-in 服务。
- Task 4：十三个真实浏览器场景验证最小建档、显式授权、关闭旧链接、账号隔离、迟到保存、键盘焦点、版本冲突、教师权限及五文件整批替换；新视觉包括手机/桌面、空/错误/加载、长内容与零小时投入。实际屏幕阅读器尚未执行，axe 不作为读屏替代。
- 全量：后端2818通过、双平台前端各704通过、完整浏览器152通过/34具名豁免、五组视觉23通过；G-4本地缺口关闭。远端当前完整CI、正式学校认证与实际读屏不在本地通过结论内。
## R4.1 追加预测（先于测试定位，2026-10-13）

| 编号 | 规约预期 | 验证位置 | 状态 |
| --- | --- | --- | --- |
| A13 | R4.1 材料事实： 仅当前公开版有效 READY 附件计入；草稿/未选上传/旧版/文字/链接不计入 | `tests/integration/innovation/test_public_talents.py::test_material_filter_counts_only_current_public_files`；`test_unreadable_current_materials_do_not_set_public_flag` | 已覆盖 |
| A14 | R4.1 撤回： 撤回最后文件后标志、详情、筛选同步变化；其他有效附件仍计入 | `tests/integration/innovation/test_public_talents.py::test_revoke_last_material_changes_flag_and_filter_immediately` | 已覆盖 |
| A15 | R4.1 查询： 有/无/全部与技能/状态/关键词组合，total及稳定分页一致，无N+1 | `tests/integration/innovation/test_public_talents.py::test_material_filter_has_stable_pagination_without_per_profile_queries`；既有多标签全部匹配用例 | 已覆盖 |
| A16 | R4.1 URL/UI： false与true均保留，返回详情/分页保留，提交重置offset；默认全部、清除筛选 | `frontend/src/__tests__/talent-query.test.ts`；`frontend/e2e/innovation-talents.spec.ts` 的 R4.1 场景 | 已覆盖 |
| A17 | R4.1 UI： 材料事实不等于认证，新生无文件仍能公开；四组字段、窄屏长内容、键盘焦点与axe | `frontend/e2e/innovation-talents.spec.ts` 最小新生与 R4.1 场景；`frontend/e2e/innovation-talents-visual.spec.ts` | 已覆盖 |
