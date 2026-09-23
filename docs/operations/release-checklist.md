# CampusQuest V1 Release Checklist

The V1 acceptance loop is spec §45 (30 criteria) of
`docs/superpowers/specs/2026-09-19-campusquest-design.md`: V1 is done only
when every row below is proven. The single command that runs the whole
automated loop is the Plan 10 release gate (docs/quality/quality-gates.md
§15):

```bash
make release-gate
```

A release requires a fresh zero-failure run of that command (rerun it after
any final fix; never reuse a previous run as evidence). The sections below
map each §45 criterion to the automated test or the manual deployment check
that proves it, then collect the operator runbook for running the gate and
the clean-environment script.

## §45 acceptance criteria → proof

Test ids are `file::test` relative to the repository root; browser tests are
`frontend/e2e/<spec>.ts › <test title>`. "e2e" files live under
`backend/tests/e2e/` and run only under `CQ_E2E=1`.

| # | §45 criterion | Proof |
|---|---|---|
| 1 | 白名单学号 + 唯一手机号注册有效 | `backend/tests/integration/identity/test_registration.py::test_register_whitelisted_student_succeeds`（白名单门）；`::test_concurrent_same_phone_exactly_one_succeeds`、`::test_concurrent_same_username_exactly_one_succeeds`（唯一性在并发下成立）；`frontend/e2e/auth.spec.ts › registers with phone OTP and lands on home after login`（浏览器全链） |
| 2 | 学号纯数字规则和 nickname 16 grapheme 正确 | `backend/tests/unit/identity/test_validation.py::test_student_number_format`（含全角/科学计数法负例）；`::test_nickname_accepts_16_zwj_family_emoji`、`::test_nickname_mixed_cjk_and_emoji_counts_grapheme_clusters`；`backend/tests/integration/identity/test_profile_contacts.py::test_change_nickname_enforces_16_grapheme_boundary`；`frontend/e2e/auth.spec.ts › nickname counter counts graphemes (emoji = 1) and caps at 16` |
| 3 | Teacher 能发布 Task 和批量导入唯一 Assignment | `backend/tests/integration/tasks/test_task_api.py::test_teacher_creates_publishes_imports_student_claims_and_abandons`；`backend/tests/unit/tasks/test_assignment_importer.py::test_duplicate_within_file_marks_later_row`、`::test_duplicate_against_database_marks_row`（导入唯一性）；schema 约束 `uq_assignments_task_id_platform_keyword`（`scripts/verify-migrations.sh` 断言） |
| 4 | 系统随机领取，不能选择 Assignment | `backend/tests/integration/tasks/test_task_api.py::test_claim_request_cannot_choose_an_assignment`；`backend/tests/integration/tasks/test_claim_concurrency.py::test_claim_snapshot_survives_later_task_edits`；`frontend/e2e/task-claim.spec.ts › detail page hides assignment payloads before claim` + `› claim allocates server-side and reveals only the user's assignment` |
| 5 | 并发领取不会重复分配 | `backend/tests/e2e/test_concurrency_gate.py::test_fifty_students_race_ten_assignments`（50 用户抢 10 单元：恰好 10 成功、0 个 500）；`backend/tests/integration/tasks/test_claim_concurrency.py::test_fifty_students_race_ten_available_assignments`；DB 兜底 `uq_assignment_claims_active_assignment` |
| 6 | 每人最多 3 个需行动 Claim，同 Task 同时最多 1 个 | `backend/tests/integration/tasks/test_claim_quota.py::test_student_at_quota_is_refused_a_fourth_actionable_claim`、`::test_second_claim_on_same_task_rejected_other_task_succeeds`；`backend/tests/e2e/test_concurrency_gate.py::test_quota_race_two_fresh_claims_land_exactly_one`、`::test_same_task_same_user_race_claims_at_most_once`；DB 兜底 `uq_assignment_claims_active_user_task` |
| 7 | 主动放弃每日最多 2 次并正确释放 | `backend/tests/integration/tasks/test_abandon.py::test_third_abandon_same_business_day_is_rejected`、`::test_counter_resets_at_shanghai_midnight_not_utc_or_24h`、`::test_concurrent_abandons_of_different_claims_cannot_bypass_limit`（并发不绕限）、`::test_abandoned_assignment_reallocated_but_never_back_to_abandoner`（正确释放回池）；`backend/tests/e2e/test_time_boundaries.py::test_abandon_daily_quota_groups_by_local_business_day` |
| 8 | FIXED / RELATIVE DDL 均工作 | `backend/tests/unit/tasks/test_deadlines.py::test_relative_mode_uses_claimed_at_plus_duration`、`::test_grace_is_exactly_24h_in_both_modes`（两模式各参）；`backend/tests/integration/tasks/test_claim_quota.py::test_fixed_task_exactly_at_cutoff_still_claims`、`::test_fixed_task_past_cutoff_refuses_claims`（FIXED 精确边界）；`backend/tests/unit/tasks/test_claim_eligibility.py::test_relative_deadline_never_cutoff_blocked` |
| 9 | CSV/XLSX/SQLite 安全上传和机器校验工作 | `backend/tests/unit/submissions/test_csv_validator.py`、`test_xlsx_validator.py`、`test_sqlite_validator.py`（逐格式正/负例）；`backend/tests/unit/submissions/test_detection.py::test_xlsx_bytes_are_detected_as_xlsx_whatever_was_declared`（声明扩展名不可信）；`backend/tests/integration/submissions/test_validation_worker.py::test_valid_csv_validates_and_persists_the_full_report`（真机器校验链）；`backend/tests/e2e/test_file_security.py::test_pathological_files_fail_with_bounded_typed_codes`（病理文件全部有界失败）；`frontend/e2e/submission.spec.ts › valid upload finalizes and reaches 待审核 with a report` |
| 10 | 自动校验通过后进入人工验收 | `backend/tests/e2e/test_happy_path.py::test_full_happy_path_chain`（VALIDATED → UNDER_REVIEW → 审批）；`backend/tests/integration/submissions/test_validation_worker.py::test_validation_start_moves_actionable_claim_to_validating`；`frontend/e2e/submission.spec.ts › valid upload finalizes and reaches 待审核 with a report` |
| 11 | DDL 奖励 100/80/50/20 边界正确 | `backend/tests/e2e/test_time_boundaries.py::test_reward_ladder_locks_exact_tier_at_every_boundary`（deadline/4h/12h/grace 全部 ±1ms + 恰好瞬时，12 参）；`backend/tests/unit/tasks/test_deadlines.py::test_reward_fraction_boundary_ladder`；`backend/tests/e2e/test_deadline_flows.py::test_plus_2h_late_submit_locks_80_percent_tier`（80）、`::test_invalidated_shell_resubmit_plus_7h_locks_50_percent`（50） |
| 12 | Teacher 审核晚不会惩罚学生 | `backend/tests/e2e/test_deadline_flows.py::test_late_teacher_review_extends_revision_window_keeps_100`（晚两天审核仍 100%） |
| 13 | Revision window 正确延长 | 同上测试（revision deadline ≥ review+24h 且改后批准保 100%）；`backend/tests/integration/submissions/test_validation_worker.py::test_failed_validation_in_revision_window_restores_revision_required` |
| 14 | 恶意空壳 reward lock 可审计地失效 | `backend/tests/e2e/test_deadline_flows.py::test_invalidated_shell_resubmit_plus_7h_locks_50_percent`（机器通过空壳失效后重交只拿 50%）；`backend/tests/integration/submissions/test_review_flow.py::test_invalidate_cancels_provisional_lock_and_audits`、`::test_invalidate_reward_lock_reason_is_mandatory`；`backend/tests/integration/submissions/test_review_audit.py::test_invalidate_audits_the_lock_migration_and_redacted_basis` |
| 15 | APPROVE 并发不重复发积分 | `backend/tests/e2e/test_concurrency_gate.py::test_concurrent_reviewers_approve_grant_exactly_once`（两审批者 → 恰一条奖励账）；DB 兜底 `uq_points_ledger_source_type_source_id_ledger_type`（verify-migrations.sh 断言） |
| 16 | 积分兑换冻结、审核、发放正确且不能双花 | `backend/tests/e2e/test_concurrency_gate.py::test_concurrent_double_spend_freezes_at_most_one`、`::test_last_stock_race_never_oversells`（余额/库存永不为负）；`backend/tests/integration/points/test_redemption_concurrency.py::test_approve_consumes_reservation_posts_entry_and_holds_stock`、`::test_concurrent_double_approve_posts_one_consumption_entry`、`::test_fulfill_is_idempotent_and_records_metadata`；`backend/tests/integration/points/test_points_api.py::test_admin_approves_then_fulfills_the_redemption`；`frontend/e2e/rewards-ranking.spec.ts › catalog renders, redemption consumes through the Admin chain` |
| 17 | 日/月/总榜基于任务贡献，不受正常兑换影响 | `backend/tests/integration/points/test_redemption_concurrency.py::test_approve_consumes_reservation_posts_entry_and_holds_stock`（兑换账 `affects_ranking=false`、earned 不动——spec §17.1）；`backend/tests/integration/rankings/test_rankings.py::test_projection_retry_converges_to_postgres_aggregate`（榜 = PG 聚合）、`::test_reversal_repairs_original_period_only`（冲销归原周期） |
| 18 | 我的附近排名工作 | `backend/tests/integration/rankings/test_growth_api.py::test_around_me_is_a_global_rank_window`、`backend/tests/integration/rankings/test_rankings.py::test_around_me_windows_global_ranks`；`backend/tests/e2e/test_ranking_recovery.py::test_rankings_rebuild_exactly_after_redis_loss`（around-me 响应在重建后逐字段相等）；`frontend/e2e/rewards-ranking.spec.ts › around-me anchors the current user by the visible 我 tag` |
| 19 | Honor 与积分资产分离 | `backend/tests/integration/rankings/test_honor_grants.py::test_earned_points_follows_the_ranking_ledger_not_the_balance`（Honor 判据走账不余额）、`::test_auto_definition_and_grant_uniqueness`、`::test_commemorative_honor_admin_only_idempotent_and_never_ranks`；`backend/tests/unit/rankings/test_honors.py::test_fixed_catalog_is_exactly_the_spec_example_set`；`frontend/e2e/rewards-ranking.spec.ts › daily/monthly/all board renders nickname/honor/score/rank only` |
| 20 | 评论支持公开/匿名、回复、赞/踩、Emoji、举报 | `frontend/e2e/community.spec.ts › an anonymous comment leaks none of its author's identity`、`› a reply nests under its root; a reply to the reply stays at level 2`、`› like shows a count, and pressing it again removes the vote`、`› an emoji reaction toggles with its aggregated count`、`› category + submit -> non-destructive confirmation, comment stays visible`；后端负搜索 `backend/tests/e2e/test_privacy_rbac.py::test_public_and_student_surfaces_exclude_sensitive_fields`、`::test_anonymous_privacy_and_explicit_reveal`；唯一索引 `uq_comment_votes_comment_id_user_id`、`uq_comment_reactions_comment_id_user_id_emoji`（verify-migrations.sh 断言） |
| 21 | 完成 Task 后可 1–5 星评分 | `frontend/e2e/community.spec.ts › the aggregate renders and a star tap produces a definite outcome`、`› a non-completer sees the typed RATING_NOT_ELIGIBLE copy`（完成者门槛）；DB 兜底 `uq_task_ratings_task_id_user_id` |
| 22 | DDL -24h/-4h 多渠道通知按策略工作并防双发 | `backend/tests/unit/notifications/test_deadline_scheduler.py::test_30h_left_plans_both_reminders`、`::test_exactly_24h_left_schedules_24h_reminder_for_now`（调度边界）；`backend/tests/unit/notifications/test_channel_eligibility.py`（按 Task policy 的渠道选择）；`backend/tests/integration/notifications/test_event_notifications.py::test_claim_creation_schedules_deadline_deliveries_in_same_transaction`、`::test_claim_at_exactly_24h_boundary_plans_reminder_for_now`、`::test_same_revision_required_key_twice_yields_one_delivery_set`（同键去重）；`backend/tests/workers/test_notification_delivery.py::test_duplicate_job_delivers_exactly_once`、`::test_concurrent_claims_produce_single_provider_call`；`backend/tests/e2e/test_worker_retries.py::test_duplicate_notification_job_delivers_exactly_once`（幂等键 == provider_idempotency_key(event_key, channel, user)）。**手动部署检查**：V1 只带 logging 供应商；接入真实 SMS/Email 供应商后需在部署环境复跑 `tests/workers/test_notification_delivery.py` 形状的真实供应商冒烟 |
| 23 | grace 到期无有效提交会释放 Assignment | `backend/tests/e2e/test_deadline_flows.py::test_no_submit_expiry_releases_assignment_for_another_student`（EXPIRED → AVAILABLE → 他人可领） |
| 24 | UNDER_REVIEW Claim 不会被错误释放 | `backend/tests/integration/tasks/test_submit_expire_race.py::test_mid_review_claims_are_protected_from_expiry`；`backend/tests/e2e/test_concurrency_gate.py::test_in_window_submission_beats_concurrent_expiry`（窗口内有效提交阻止并发释放） |
| 25 | 文件按 Task retention policy 清理 | `backend/tests/unit/submissions/test_upload_policy.py::test_retention_snapshot_dated_policies`（快照语义）；`backend/tests/integration/submissions/test_upload_finalize.py::test_retention_snapshot_at_finalize_for_all_policies`；`backend/tests/workers/test_cleanup_deletion_claim.py::test_real_repository_claim_reevaluates_every_guard`；`backend/tests/e2e/test_worker_retries.py::test_duplicate_cleanup_job_reconciles_without_metadata_loss`（对象已删的 reconcile 不丢元数据） |
| 26 | Teacher/Admin 权限隔离有效 | `backend/tests/e2e/test_privacy_rbac.py::test_direct_route_rbac_matrix`（三角色 × 五路由类精确状态码矩阵）；`backend/tests/integration/admin/test_admin_api.py::test_teacher_direct_call_is_permission_denied`（admin 面 teacher 全拒）；`backend/tests/integration/identity/test_account_status.py::test_confirmed_teacher_denied_the_admin_guard`、`::test_student_denied_staff_operation`；Teacher Task-scope：`backend/tests/integration/tasks/test_task_api.py::test_staff_roles_cannot_claim` |
| 27 | 高风险动作有 AuditLog | `backend/tests/e2e/test_privacy_rbac.py::test_anonymous_privacy_and_explicit_reveal`（reveal 带 reason → `COMMUNITY_IDENTITY_REVEAL` 行，缺 reason 拒）；`backend/tests/integration/admin/test_account_admin.py::test_legal_transition_commits_and_audits_in_one_transaction`（停用/封禁/复活同事务审计）；`backend/tests/integration/points/test_points_api.py::test_redemption_decisions_write_durable_audit_rows`；`backend/tests/integration/audit/test_audit_log.py::test_writer_appends_one_row_committed_by_the_caller` |
| 28 | 关键错误使用稳定 code | `backend/tests/unit/core/test_error_codes.py::test_enum_membership_matches_frozen_registry`（注册表冻结）；`backend/tests/unit/core/test_errors.py::test_business_error_has_stable_envelope`；前端 typed copy：`frontend/e2e/rewards-ranking.spec.ts › conflict shows typed INSUFFICIENT_POINTS copy and stays retry-friendly`、`frontend/e2e/task-claim.spec.ts › conflict shows typed copy and stays retry-friendly` |
| 29 | Redis 丢失后排行榜可从 PostgreSQL 重建 | `backend/tests/e2e/test_ranking_recovery.py::test_rankings_rebuild_exactly_after_redis_loss`（真 DEL ranking:* → 真 rebuild → 四响应逐字段相等）；`backend/tests/integration/rankings/test_rankings.py::test_redis_loss_rebuild_restores_identical_results`、`::test_rebuild_evicts_stale_members` |
| 30 | 关键并发、边界和 Worker 重试测试全部通过 | `make release-gate`（本清单的门禁命令，含 e2e 全套）；并发专项：`backend/tests/e2e/test_concurrency_gate.py` 七测（Plan 10 T5 曾以五连跑验证零 flake）；边界专项：`backend/tests/e2e/test_time_boundaries.py`；Worker 重试专项：`backend/tests/e2e/test_worker_retries.py` 五测 |

## Operator runbook

### Running the gate

```bash
# once: bring the dependency stack up (PostgreSQL 15432, Redis 6379, MinIO 9000)
docker compose -f infra/docker-compose.yml up -d

make release-gate
```

The gate self-bootstraps its test database: the chain's FIRST step
(`release-test-db`) runs an idempotent `alembic upgrade head` against
`campusquest_test` (the TEST_STACK_ENV default), so a brand-new compose
stack with empty volumes goes straight into `make release-gate` — fresh
volumes → `up -d` → gate exit 0 is the Task-1 release proof. A
pre-migrated database makes the step a no-op version check.

The gate is thirteen ordered steps (Makefile `release-gate`); the
playwright step additionally asserts zero skipped tests in the
teacher/admin suites (`frontend/scripts/assert-e2e-no-skips.mjs` — a
missing world export reads as skips, never as a silent green). Steps
12–13 (ranking rebuild, concurrency gate) run inside the backend e2e
suite (`tests/e2e/test_ranking_recovery.py`,
`tests/e2e/test_concurrency_gate.py`)
— see the Makefile comment for the full mapping. `CQ_S3_SMOKE=1` and
`CQ_COMPOSITION_SMOKE=1` are set by the integration target itself, so the
real-stack smokes never silently skip inside the gate.

### Clean-environment verification (destructive)

```bash
# deletes the compose volumes — dev database AND MinIO bucket data
CQ_CLEAN_START_CONFIRM=YES bash scripts/verify-clean-start.sh
```

The script refuses to run without the confirmation (or `--yes`), resolves
the exact container/volume set the compose file would remove, and requires
every name to be campusquest-prefixed before issuing `down -v`. It then
brings the stack up, migrates the dev database, seeds demo accounts
(`backend/scripts/seed_demo_accounts.py`), starts API/worker/frontend with
readiness polls, and tears down everything it started (trap).

Migration-only verification is non-destructive and uses its own database
(`campusquest_migrate_test`, dropped on exit):

```bash
bash scripts/verify-migrations.sh
```

### Resetting the test stack (debris from killed runs)

The gate assumes EXCLUSIVE use of the compose test stack and a
`campusquest_test` database free of orphaned rows. Browser-e2e runs commit
real rows and rely on `globalTeardown` to remove them; a killed Playwright
run leaves its world behind, and directory-style tests that assert on an
unfiltered first page (username-ordered, page size 20) break once ~20
orphaned users exist (`tests/integration/admin/test_admin_api.py::test_user_directory_lists_and_filters`
was taken down this way by seven orphaned browser worlds — observed in
Plan 10 E5). The database and Redis DB 0 are disposable by design; restore
the precondition with CI's own procedure:

```bash
docker compose -f infra/docker-compose.yml exec -T postgres psql -U campusquest -d campusquest \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='campusquest_test' AND pid <> pg_backend_pid()" \
  -c "DROP DATABASE IF EXISTS campusquest_test" -c "CREATE DATABASE campusquest_test OWNER test"
docker compose -f infra/docker-compose.yml exec -T redis redis-cli -n 0 FLUSHDB
cd backend && DATABASE_URL=postgresql+asyncpg://test:test@localhost:15432/campusquest_test \
  REDIS_URL=redis://localhost:6379/0 S3_ENDPOINT_URL=http://localhost:9000 \
  S3_BUCKET=campusquest-test S3_ACCESS_KEY=campusquest S3_SECRET_KEY=campusquest-dev \
  BUSINESS_TIMEZONE=Asia/Shanghai uv run alembic upgrade head
```

Concurrent sessions on one host share this stack; before a release run,
make sure no other Playwright/pytest session is seeding worlds into
`campusquest_test`.

### Runbook pitfalls (observed in Plan 10 E4, all reproducible)

1. **Port squatting defeats `reuseExistingServer`.** The Playwright config
   reuses whatever already listens on the configured ports. When unrelated
   processes occupy 3000 (another app) or 8000 (another checkout's
   uvicorn), the suite silently tests the wrong servers. Move both with
   `CQ_E2E_BASE_URL` / `CQ_E2E_API_URL` to free ports — the config derives
   the server ports and the frontend's `CQ_DEV_API_PROXY` from the same
   variables, so one override moves client and servers together.
2. **`CQ_E2E_API_URL` must include the `/api/v1` path.** An override with
   a bare origin makes every APIRequestContext call 404 (NOT_FOUND) — the
   backend serves under `/api/v1`. Symptom: all staff minting / approval /
   report reads suddenly 404 while page loads still work.
3. **The login window is a real budget.** Backend rate limit
   `auth:login` is 10 form logins / 5 min / user+IP. Suites that log the
   same student in repeatedly (community, task-claim) must go through the
   Playwright `ensureStudentLogin` cookie-resume chain
   (`frontend/e2e/fixtures.ts`) instead of the form, or downstream specs
   429 at the login page. Login-UX coverage stays in auth.spec.
4. **A non-3000 frontend origin must be in MinIO's CORS allowlist.** The
   browser upload proof PUTs submission files straight to MinIO
   cross-origin, so moving `CQ_E2E_BASE_URL` off port 3000 also requires
   the new origin in the running `MINIO_API_CORS_ALLOW_ORIGIN` (the
   compose default covers only localhost:3000/127.0.0.1:3000):
   `MINIO_API_CORS_ALLOW_ORIGIN=<existing>,http://localhost:<port>,http://127.0.0.1:<port> docker compose -f infra/docker-compose.yml up -d minio`
   (observed in Plan 10 E5; the allowlist extension does not touch the
   data volume).
5. **Next 16 allows only ONE dev server per project directory.** A
   leftover `next dev` from a previous session in the same frontend
   directory makes every new `next dev` exit immediately with "Another
   next dev server is already running" — regardless of the port — which
   takes down the Playwright webServer step. The error names the stale
   PID (and writes `.next/dev`); kill that process before the gate run
   (observed in Plan 10 E5: an E4-era dev server on 3311 blocked a fresh
   server requested on 3457).

### Known coverage gaps at V1 (not blockers, tracked)

- `frontend/e2e/admin.spec.ts` / `teacher.spec.ts` operations flows run
  for real since the staff-fixture contract landed (`CQ_E2E_STAFF` /
  `CQ_E2E_STAFF2` / `CQ_E2E_TEACHER` / `CQ_E2E_ADMIN` + base32 TOTP
  world exports, browser_world.py); the release gate's playwright step
  asserts both suites at zero skips.
- V1 ships logging-only SMS/Email adapters by design; real-provider
  delivery is a post-V1 deployment check (row 22).
- **Flake watch (E5, root cause narrowed, fix pending owner decision):**
  `frontend/e2e/staff-auth.spec.ts › invite -> password -> TOTP confirm ->
  recovery codes once -> done` failed once in a cold-server full-gate run
  with every code rejected (`动态验证码错误` loop). The setup effect in
  `frontend/src/features/auth/TotpSetup.tsx` calls the rotating
  `/staff/totp/begin` on mount; under `next dev` React StrictMode
  double-invokes it, and when the two overlapping rotations commit in the
  opposite order to their responses the DOM shows a secret the server no
  longer stores — every confirm is then wrong regardless of retry
  (isolation passes 4/4; rerun is a valid gate recovery). Candidate fix:
  a single-flight guard so only one begin runs per mount.
