# CQ-REFACTOR-PORT-01 — 移植 CQ-REFACTOR-01 的仍有效部分到 main

- 日期：2026-10-27（同日延伸批：`_login_succeeded` 结构收敛）
- 分支：`health/channel-audit`（基线 `origin/main@56ef06f`，worktree `.claude/worktrees/campusquest-port-main`）
- 源材料：本仓库对象库中的本地提交 `b03d8f5`（重构本体，做在落后的 ci-pipeline-hardening 基线上）与 `7cea962`（协作档案）。前端组件抽取部分（StaffCodeLogin 家族）已被 main 的 unified-login 批次取代，明确不在本批范围。

## 延伸批（2026-10-27 第二批令）：`_login_succeeded` 结构收敛

- **契约先行（改前落笔）**：①审计行——action=EMAIL_LOGIN_SUCCEEDED、actor=Actor(user_id, Role(user.role))、target=user/self、after_snapshot 三变体（`{"channel": "email_code"}` / +`remembered_device: True` / `{"channel": "email_code+totp"}`）、ip/request_id 穿线（钉在 test_login_audit_rows.py 五形态 + AL10）；②INFO 日志渲染文本 `email login succeeded user_id=<id> channel=email_code[+device|+totp]`（钉在 caplog 断言）；③Outcome——tokens=issue_session(user, now)、device_cookie/slot 仅 remember 时铸造否则 None、commit 先于 return。
- **改法**：verify_code 的 teacher 腿与 remembered-device 腿（main 仍近逐行重复，批令核实属实）+ verify_totp 的 totp 成功腿（同型，已确认）全部收敛为 `_login_succeeded(channel=..., used_remembered_device=...)`，参照 b03d8f5 模板适配 main 演化：`issue_device(db, user, clock=...)` 的 `(cookie, slot)` 元组穿线、两值 `or None` 归一；helper 顶部（commit 前）读 `user.id`/`user.role`，守住 no-post-commit-attribute-access 规则。日志以 `%s` 渲染 channel（模板同法），渲染文本逐字不变。
- **RED/GREEN（行为保持批：保护网先绿后零修改复绿，非新增行为的 RED）**：
  - 基线（1e8ebac，树净）：`pytest tests/integration/identity/test_login_audit_rows.py` → 0（4 passed）；
  - 重构后同一命令 → 0（4 passed），`git diff -- tests/` 零改动；
  - identity 集成 310 passed（exit 0）；identity 单测（CI 形态环境）144 passed（exit 0）；ruff format/check/mypy 单文件 0/0/0。
- **提交**：`4c21f6b` refactor(identity): funnel the email-login success legs through _login_succeeded（+72/-72，纯收敛零行数变化）。

## 移植了什么

1. **渠道审计 → 零生产代码移植（main 已有，逐字等价）**
   - 核实：`git log -S 'channel=email_code'` 与 `-S '"channel": "email_code"'` 均溯源到 `92d23f9`（staff email-code login endpoints，main 祖先）。b03d8f5 的 diff 中渠道审计全部位于其父基线的删除行一侧——它把三个内联成功点收敛为 `_login_succeeded` 且 "audit snapshot and INFO log kept verbatim"，不是新增审计。
   - main@56ef06f 的 `backend/app/modules/identity/email_login_service.py` 三个成功路径（teacher :396 / remembered device :427 / totp :554）已含 INFO 日志 `channel=email_code` / `email_code+device` / `email_code+totp` 与 AuditLog `EMAIL_LOGIN_SUCCEEDED` after_snapshot（设备路径带 `remembered_device: True`），与 b03d8f5 重构后可观察行为逐字一致。
   - **不移植，因 main 已有**：上述全部生产代码。登录语义/错误码/状态机零改动。
2. **测试增量（本批唯一真实代码增量）**：`backend/tests/integration/identity/test_login_audit_rows.py`
   - after_snapshot 断言：main 已有且更全（teacher/totp/device 三变体 + mobile 两变体，:306/:377/:404）——不移植，不重复钉。
   - **缺失增量 = caplog INFO 日志断言**：teacher 测试钉 `channel=email_code`；admin 测试钉 `channel=email_code+totp`（leg 2）+ `caplog.clear()` + `channel=email_code+device`（leg 3）。参照 b03d8f5 在 test_email_code_login_api.py 的断言形态，适配 main 的测试结构（该文件已随 PR-3 路由退役消失，审计行的家现在是 test_login_audit_rows.py）。
   - **RED 不可得的说明**：行为已在 main 存在（92d23f9），正确断言落笔即绿。作为咬合证据做了断言负控制——临时把 device 断言期望改为 `channel=email_code+magic_red_control`，admin 测试 FAIL（日志流显示实现发出 `channel=email_code+device`），改回后 GREEN。
3. **docs/best-practices/refactoring.md**：从 b03d8f5 逐字节移植（`git diff --no-index` 空差证明）。AGENTS.md Development discipline 末尾加同一行指针，位置与 b03d8f5 一致；未引用未合入的 testing-ci.md。
4. **admin 两处小改**：核对后问题在 main 仍存在，均移植——
   - `frontend/e2e/admin-powers.spec.ts:15`：`type Page` 导入无使用 → 删除；
   - `frontend/src/features/admin/SystemAdmin.tsx:1035`：release 分支 `const result` 未读（force-fail 分支合法使用 `result.attempts`）+ 常量串跨行 → 收敛。

## 丢了什么及原因

- **前端组件抽取**（StaffCodeLogin.tsx / api.ts / staffEmailLoginView.ts 等）：范围明确排除——main 的 unified-login 批次已取代该形态。
- **渠道审计生产代码**：main 已有等价实现（见上），按批令"main 已有则不移植"。
- **b03d8f5 的 email_login_service.py 三成功点收敛（`_login_succeeded` helper）**：首批令范围仅渠道审计，未评估、未移植（当时注记"main 无重复收敛需求"）；同日延伸批令核实 main 的 teacher/remembered-device/totp 三腿仍近逐行重复，**已按延伸批移植**（见文首"延伸批"节，提交 `4c21f6b`）。
- **b03d8f5 的 after_snapshot 测试断言**：main 的 test_login_audit_rows.py 已有更强断言（五种 channel 形态全钉 + G11），不重复。

## 命令与退出码

| 步骤 | 命令 | 退出码 |
|---|---|---|
| 负控制 RED | `sed 换 magic 期望` + `COVERAGE_FILE=/tmp/cq-port.coverage uv run pytest tests/integration/identity/test_login_audit_rows.py -k "teacher_email_code_login_writes or admin_email_login_writes"` | pytest 1 failed 1 passed（管道后 tail EXIT=0 为 tail 的；pytest 实际非零） |
| GREEN（文件全量） | 同上（正确断言，无 -k） | 0（4 passed） |
| identity 集成门禁 | `uv run pytest tests/integration/identity -q`（共享 PG 15432，独立 COVERAGE_FILE） | 0（310 passed，63s） |
| identity 单测（裸跑） | `uv run pytest tests/unit/identity -q` | 1（3 failed：test_cookie_paths ×3，`Settings()` 缺 business_timezone 等——新 worktree 无未跟踪 backend/.env；stash 原始基线复跑同样 3 failed，先在环境性失败） |
| identity 单测（CI 形态环境） | `DATABASE_URL=... REDIS_URL=... S3_* ... BUSINESS_TIMEZONE=Asia/Shanghai uv run pytest tests/unit/identity -q`（值取自 tests/integration/db_guard.py 的已提交默认） | 0（144 passed） |
| 静态三连（触碰测试文件） | `ruff format --check` / `ruff check` / `mypy`（单文件） | 0 / 0 / 0 |
| 矩阵 checker | `uv run python scripts/check_test_matrix.py ../docs/quality/test-matrix/audit-log.md` | 0 |
| npm ci（worktree 内装依赖） | `npm ci` | 0 |
| 前端 typecheck / lint | `npm run typecheck` / `npm run lint` | 0 / 0 |
| 前端 test:unit | `npm run test:unit` | 1：866 中 1 失败（auth-transition-serialization "logout drains the in-flight refresh"）；stash 原始基线复跑同样失败、复跑稳定复现 → main@56ef06f 先在失败，与本批无关，已上报不扩修 |
| make pre-push（本机默认 node v24.22.1） | `make pre-push` | 2（Makefile:91）：前端腿 `coverage:ratchet` 内部 `tsx --test --experimental-test-coverage` 被上述同一先在失败测试拖死（ratchet 比对地板前即非零退出）；ratchet-direction/arch/矩阵/契约新鲜度等前置步全绿 |
| make pre-push（PATH 限定 Node 22.20.0 = CI 钉版，本机 /usr/local/n/versions/node/22.20.0） | `PATH=/usr/local/n/versions/node/22.20.0/bin:$PATH make pre-push` | **0（全绿）**：ratchet-direction OK、ruff format/check+mypy、import-linter ×11、depcruise（0 err）、check_test_matrix 28 文件 OK、G3 guard、OpenAPI 快照 `git diff --exit-code` 干净 + api:types:snapshot 零漂移、typecheck/lint/check:css、coverage ratchet OK（lines 95.39 / branches 92.29 / functions 90.7，地板 94.99/91.78/90.63 未降） |

### 环境发现（上报，不扩修）：auth-transition 测试对 Node 运行时敏感

`src/__tests__/auth-transition-serialization.test.ts` 三态实测：Node 18 整文件挂（`Promise.withResolvers is not a function`——该文件要求 ≥22）；**Node 24（本机默认）仅测试 A 挂**（`0 !== 1` 时序断言，stash 基线同样挂=先在）；**Node 22（CI 钉版）全套绿**。本机默认 node 与 CI 钉版不一致，本地跑前端电池/pre-push 应 `PATH=/usr/local/n/versions/node/22.20.0/bin:$PATH`（或等效方式固定 22）。

## G19 测试工件

- 矩阵：`docs/quality/test-matrix/audit-log.md` AL10 证据列扩展（日志面钉住 + 溯源注记），checker 过。预期列保持 spec §30 原推导不动——spec 未明文要求日志面，证据列明确标注为实现契约钉住。批令建议的 login-iteration.md / identity-rbac.md 经核对无 channel/日志相关行（login-iteration 零命中；identity-rbac 仅无关的邮箱约束行），正确落点是 audit-log.md（test_login_audit_rows.py 的 docstring 自证归属）。
- 无新 e2e/像素/选择器/豁免/API 面变更：其余工件清单项不适用。

## not-run（留待未来 PR 前）

- `make verify` / `make release-gate`：批令明确本批不跑。
- 后端 coverage ratchet（-cov 积累链）/ e2e 电池 / 像素回归：属 release-gate/CI 层，同上。
- admin-powers.spec.ts 的 e2e 电池：改动仅删未用导入，无行为面；跑电池需借 3000 栈（owner 栈正被另一会话恢复），不值得。

## 全量门禁（CQ-PORT-STACK-01 窗口，2026-10-27 22:39 放行）

窗口 ack：`notes/collaboration/CQ-PORT-STACK-01-ack.md`（根检出，campus reviewer 落盘）。两跑均新鲜整跑、Node 22（`PATH=/usr/local/n/versions/node/22.20.0/bin:$PATH`）。

| 门禁 | 命令 | 退出码 | 结果 |
|---|---|---|---|
| make verify | `PATH=node22 make verify`（日志 /tmp/cq-port-verify.log） | **0** | backend 全量 3144 passed / 54 skipped（377.74s；e2e 标记类按 verify 形态设计性跳过）；frontend typecheck/lint/check:css/build 全绿 |
| make release-gate | `PATH=node22 make release-gate`（日志 /tmp/cq-port-releasegate.log） | **2** | 红于 `playwright-e2e` 腿（Makefile:425）：电池 213 passed / 37 skipped / **1 failed**（15.5m），其余腿到该点全绿（backend unit+worker cov、integration cov、backend-e2e、migration-verify、前端静态/单测/build 已过） |

### release-gate 红因（确定性、先在于 main、与本批零重叠）——不可收口，停下上报

- 失败测试：`e2e/notifications.spec.ts:50`「anonymous visitor › /notifications shows the login CTA, not a broken page」——期望匿名访问 /notifications 就地渲染 StudentShell 的 CTA 壳（`去登录` 链接，`data-shell="anonymous"`）。
- 实际行为：页面快照（error-context.md）显示渲染的是**真正的 /login 登录表单**（账号输入框/获取验证码/忘记密码）。根因：`327fd5a`（feat(shell): no-evidence direct path，#145 首屏直跳批）在 `frontend/src/app/(student)/layout.tsx:49` 对无 csrf cookie 且无 cq_shell hint 的访客**服务端 `redirect("/login")`**（`shellRedirectsToLogin`，shellChrome.ts:111）。Playwright 全新上下文必触发 → `登录 CampusQuest` H1 在登录页同样存在故 :38 过、`去登录` 链接不存在故 :55 挂。
- 确定性证据：①电池内失败一次；②隔离复跑 `npx playwright test notifications.spec.ts --grep "login CTA" --project=chromium`（同 env 形态）**同样失败**（ISOLATED_EXIT=1，同 locator 同缺失）；③spec 最后修改 2026-10-10（cf14afc，早于 327fd5a），从未随新行为更新——与「CI 停摆期 e2e 债」（2026-10-07 起 CI 暂停，本地电池未覆盖该批）记忆吻合。
- 裁决归属：修复=改 spec 断言适配 327fd5a 的新产品行为，或 owner 调整重定向——产品语义决定，非本移植批范围（批令：范围外问题记录上报不扩修）。整门重跑必再红于此，未重跑（两跑不拼接原则下无意义消耗窗口）。

### 现场清理（留档）

- 我起的进程全拆：Playwright webServers（3000/8000）随电池/隔离跑退出自动拆除，3000/8000/8100 复验全空闲；无游离 next/uvicorn/playwright 进程。
- 世界清零：`make reset-test-db`（exit 0）——campusquest_test drop/recreate + alembic 至 0045_recruitment_notifications（=ack 交付形态），users=0 复验，Redis db0 随 target FLUSHDB。
- 依赖容器（15432/6379/9000/ClamAV evidence）全程在线未动；未重启常驻栈（按 ack 归还方式：通告发 campus reviewer 由其 stack.sh start 恢复）。
- worktree 干净 @ 4bcbd2e（test-results/.coverage 均为 gitignored 工件）。

## 全量门禁·续窗（CQ-PORT-STACK-02，2026-10-28 00:03 放行）——**两门禁全绿**

对象前移至 `6d9e956`（= 712fb48 merge origin/main@7307a5a，#149/#150 并入，含上窗红腿的 307 语义重写 `3293ec5/719d70c test(e2e): align anonymous inbox access with login redirect`、isolation.ts 夹具隔离批与 noskip 豁免清理）。ack：`notes/collaboration/CQ-PORT-STACK-02-ack.md`。同 Node 22 纪律、零拼接。

| 门禁 | 退出码 | 关键数字 |
|---|---|---|
| `PATH=node22 make verify` | **0** | backend 全量 **3183 passed / 54 skipped**（368.00s，较上窗 +39 = #149/#150 增量）；前端 typecheck/lint/check:css/build 绿 |
| `PATH=node22 make release-gate` | **0** | unit+worker cov **1621**（2:10）；integration cov **1571 passed / 8 deselected**（9:16）；backend-e2e **47**（1:04）；Playwright 主电池 **216 passed（15.3m）**；visual-regression **18**（46.4s）；innovation-visual **8**（1.1m）；no-skip 守卫过；coverage ratchet OK（lines 95.38 / branches 92.29 / functions 90.7 vs 地板 94.99/91.78/90.63） |

**上窗红腿转绿实证**：电池输出 `✓ 131 [chromium] e2e/notifications.spec.ts:58:7 anonymous visitor (the server-side 307 handoff) › /notifications is handed to /login server-side — no inbox, no stale CTA (427ms)`——重写后以 `page.request.get(maxRedirects:0)` 断言 307+location，与 327fd5a 服务端直跳语义对齐；上窗「不可收口」裁决由 main 侧修复兑现。

### 现场清理（续窗留档）

- 3000/8000 随电池退出释放，8100 全程未占用；无游离 next/uvicorn/playwright 进程。
- `make reset-test-db`（exit 0）：campusquest_test 回 head=0045、users=0，Redis db0 FLUSH。
- 依赖容器在线未动；常驻栈未重启（归还通告发 campus reviewer，由其 stack.sh start 恢复）。
- worktree 干净（test-results/.coverage 为 gitignored 工件）。

## 提交清单（本批 HEAD = 见 git log；基线 56ef06f）

1. `ddfd5a7` test(identity): pin the login-success INFO log channel shorthand via caplog（测试增量 + AL10 矩阵）
2. `3966a24` docs: port the behavior-preserving refactoring practices from CQ-REFACTOR-01（refactoring.md 逐字节 + AGENTS 指针）
3. `ebc3f7e` fix(admin): drop the dead Page import and the unused release result binding（admin 两处）
4. `1e8ebac` docs(notes): 本记录首版
5. `4c21f6b` refactor(identity): funnel the email-login success legs through _login_succeeded（延伸批结构收敛）
6. `4bcbd2e` docs(notes): 延伸批修订
7. `712fb48` docs(notes): 门禁窗口 01 结果（verify 绿 / release-gate 红因留档）
8. 续窗 02 修订：head 前移 6d9e956 后两门禁全绿（见「全量门禁·续窗」节）

## 环境注记

- 共享容器（PG 15432 / Redis 6379 / MinIO 9000 / ClamAV）在线未动；owner 常驻栈（3000/8100）由另一会话恢复中，未占用。诊断性 pytest 均带独立 `COVERAGE_FILE=/tmp/cq-port.coverage`。
- worktree 自建 `backend/.venv`（uv）与 `frontend/node_modules`（npm ci），均为本 worktree 内未跟踪产物。
