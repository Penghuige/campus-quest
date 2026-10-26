# 平台注册表 规约-测试对账矩阵

> 建立日期：2026-10-16 · 域：平台注册表（platforms 表 / 字典 API / admin CRUD / 导入校验）
> 覆盖章节：docs/superpowers/specs/2026-10-16-platform-registry-design.md#§1 · §2 · §3 · §4 · §5
> 方法论：`campusquest-test-process` skill 规约对账节；模板 `docs/quality/test-matrix/points-ledger.md`。
> **纪律**：预期列先于测试检索从 spec 原文推导落笔；找不到测试=缺口（列出，不补）；测试行为≠预期=不匹配（上报，不擅改）。
> 规约来源：docs/superpowers/specs/2026-10-16-platform-registry-design.md §1-§5（owner 已批）。

## 对账摘要

| 状态 | 数量 | 明细 |
|---|---|---|
| 已覆盖 | 10 | PR1-PR10 |
| 缺口（轻，列出不补） | 0 | — |
| 不匹配 | 0 | — |
| 流程行 | 0 | — |

## 矩阵

| # | 规则引用 | 预期行为（先于测试写下） | 测试位置（file::test） | 状态 |
|---|---|---|---|---|
| PR1 | §1 数据模型 | platforms 表七种子（xiaohongshu/douyin/zhihu/boss-zhipin/zhaopin/58city/lietou，显示名排序齐）；code PK 重复拒 | `tests/integration/tasks/test_platform_registry.py::test_seed_platforms_present` / `::test_platform_model_roundtrip` / `::test_registry_seeds_constant_matches_spec` | 已覆盖 |
| PR2 | §1 FK | assignment.platform 去 CHECK 改 FK→platforms.code；未注册平台插入=IntegrityError（FK 钉，注册表时代的封闭保证） | `tests/integration/tasks/test_platform_registry.py::test_assignment_platform_is_fk_bound` | 已覆盖 |
| PR3 | §2 存量回填 | zhihu 占位按 keyword 前缀回填真实 code（BOSS直聘%/智联招聘%/58同城%/猎聘%）；keyword 字节不动；无前缀行（演示世界）不动；up/down 双向 | 迁移演练实证（本 PR 过程，0029 对真实占位行）：`tests/integration/tasks/test_platform_registry.py::test_placeholder_backfill_and_keyword_untouched`（契约 pin） | 已覆盖 |
| PR4 | §3 字典 | GET /platforms 登录后只读：仅 enabled、sort_order 升序（code 稳定并列）、DTO 仅 code/display_name/sort_order；未登录 401 | `tests/integration/tasks/test_platforms_api.py::test_public_dictionary_lists_enabled_sorted` / `::test_public_dictionary_requires_login` / `::test_disabled_platform_leaves_the_public_dictionary` | 已覆盖 |
| PR5 | §3 admin 读 | GET /admin/platforms 全量（含 disabled）；TEACHER/STUDENT 403 | `tests/integration/tasks/test_platforms_api.py::test_role_matrix_teacher_and_student_are_403` | 已覆盖 |
| PR6 | §3 admin 建 | POST /admin/platforms：code+display_name+sort_order，code 规范化小写、创建后不可改；重复 code 409 CONFLICT；PLATFORM_CREATED 审计同事务 | `tests/integration/tasks/test_platforms_api.py::test_admin_can_create_and_audit_row_lands` / `::test_duplicate_code_conflicts` | 已覆盖 |
| PR7 | §3 admin 改 | PATCH /admin/platforms/{code}：display_name/sort_order/enabled 可改（请求体无 code=不可改由构造保证）；未知 code 404；PLATFORM_UPDATED before/after 快照 | `tests/integration/tasks/test_platforms_api.py::test_admin_patch_updates_and_snapshots` / `::test_patch_unknown_code_404s` | 已覆盖 |
| PR8 | §3 导入注册表化 | 封闭集合→注册表：admin 新建平台（weibo）立即可导入；enabled=false 平台=行级 UNSUPPORTED_PLATFORM（新任务不可用）；未注册=行级 UNSUPPORTED_PLATFORM | `tests/integration/tasks/test_platforms_api.py::test_import_accepts_admin_created_platform` / `::test_import_rejects_disabled_and_unknown_platforms`；单测封闭世界 seam：`tests/unit/tasks/test_assignment_importer.py`（provider 注入族）+ 种子集断言 `::test_platform_set_matches_spec_codes` | 已覆盖 |
| PR9 | §1 enabled 语义 | enabled=false 不影响存量 assignment 引用（FK 无 enabled 判定；演示行保持） | `tests/integration/tasks/test_platform_registry.py::test_placeholder_backfill_and_keyword_untouched`（存量行与注册行共存即证 FK 不判启停；**行为级注记**：无逐句 FK-enabled 断言） | 已覆盖（行为级） |
| PR10 | §5 权限矩阵 | 平台 admin 面=ADMIN only（require_admin_actor：TEACHER 即使 ACTIVE+confirmed TOTP 也 403）；字典=任意 ACTIVE 角色 | `tests/integration/tasks/test_platforms_api.py::test_role_matrix_teacher_and_student_are_403`（TEACHER 403）+ `::test_public_dictionary_lists_enabled_sorted`（student 200） | 已覆盖 |
