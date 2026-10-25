# 测试报表面板（`make test-report`）

离线、单文件的测试结果总览：每层一行，每个测试一个方块；点击方块看详情。设计稿为 `dashboard-2026-10-25`（owner 2026-10-25 确认），生成器是 `backend/scripts/test_report.py`（Python 标准库，零新增依赖）。

```bash
make test-report
# 产物：
#   reports/test-report.html      ← 浏览器直接打开，离线可用
#   reports/index.html            ← 查看器首页（最新报告 + 历史入口）
#   reports/history/<短sha>/      ← 每个 commit 一份归档（HTML + meta.json）
#   reports/artifacts/*.xml|json  ← 聚合输入（含 ci.json）
```

`reports/` 已加入 `.gitignore`，不会进入提交。

## 0. 随服务启动 + 独立端口查看

```bash
make stack        # docker compose up -d + 最新 commit 报告 + 查看器
# 打开 http://127.0.0.1:8200/  （只绑本机）
make stack-down   # 只停查看器；依赖栈继续运行
```

`make stack` 是本地起服务的规范入口：它不会修改 `infra/docker-compose.yml`（不新增容器、不占共享栈端口、没有 bind-mount 属主陷阱）。查看器是标准库 `python3 -m http.server`，端口可用 `REPORT_PORT=8300 make stack` 覆盖。若 8200 已有查看器在响应，`make stack` 会复用它而不是重复启动。

## 1. `make test-report` 的行为

- `reports/artifacts/` 已有结果文件 → 直接聚合，不重复跑。
- 缺后端 unit+worker 的 JUnit XML → 先跑 `tests/unit tests/workers` 生成。
- 缺前端 unit XML 且 `frontend/node_modules` 存在 → 用 Node 22 内置 `node:test` + junit reporter 生成。
- `frontend/node_modules` 不存在 → **不执行 `npm ci`**；前端单测层显示“未运行”，报告顶部注明原因。
- 后端集成、后端全栈 e2e、Playwright、像素回归需要 PostgreSQL/Redis/MinIO/浏览器：**不自动跑，也不借栈**。缺结果时该层显示“未运行”。
- **CI 记录**：生成前用 `gh` 拉取当前 commit 的 GitHub Actions 记录（`backend/scripts/ci_record.py`），页面顶部显示三个必需 job 的绿/红/等待状态与运行链接。无记录（本地未推送）、`gh` 缺失或断网都会降级为中性提示，不阻断生成。
- **按 commit 归档**：每次生成把页面与 `meta.json` 写入 `reports/history/<短sha>/`，并重建 `reports/history/index.html`；默认保留最近 20 个 commit（`--keep-history` 可调）。工作区有未提交改动时，报告顶部注明“CI 记录仅对应 commit”。
- 退出码：报告写出返回 0（即使有红块——报告本来就是用来看红的）；结果文件解析失败、HTML 写入或归档失败返回非 0。
- 刷新：`rm -rf reports/artifacts && make test-report`。

## 2. 七层与结果文件

| 层 | 结果文件（`reports/artifacts/`） | 产生方式 |
|---|---|---|
| 1. 后端单测 + worker | `backend-unit.xml` | `make test-report` 自动补跑 |
| 2. 后端集成 | `backend-integration.xml` | 手动；需要真实 PG/Redis/MinIO |
| 3. 后端全栈 e2e | `backend-e2e.xml` | 手动；需要完整栈 |
| 4. 前端单测 | `frontend-unit.xml` | `make test-report` 在依赖存在时自动补跑 |
| 5. Playwright 电池 | `playwright.json` | 手动；需要完整栈 |
| 6. 像素回归 | `playwright.json` | 同文件；spec 文件名含 `visual` 的用例自动归入本层 |
| 7. 探索层 | `exploration.json` | 探索 agent 写入；无文件则显示未运行 |

手动生成示例（在测试栈已启动的前提下；环境变量按 `make release-gate` / Makefile 的 `TEST_STACK_ENV` 取）：

```bash
# 后端集成 / e2e：在对应 pytest 命令后追加 --junitxml
cd backend && uv run pytest tests/integration -m integration \
  --junitxml=../reports/artifacts/backend-integration.xml
cd backend && CQ_E2E=1 uv run pytest tests/e2e \
  --junitxml=../reports/artifacts/backend-e2e.xml

# 前端单测（与 make test-report 注入 reporter 的命令一致）
cd frontend && node --import tsx --test \
  --test-reporter=junit \
  --test-reporter-destination=../reports/artifacts/frontend-unit.xml \
  src/__tests__/*.test.ts

# Playwright：用内置 JSON reporter；visual spec 自动进第 6 层
cd frontend && PLAYWRIGHT_JSON_OUTPUT_NAME=../reports/artifacts/playwright.json \
  npx playwright test --reporter=json
```

`frontend/playwright.config.ts` 本身也会写 `frontend/test-results/report.json`；如果刚跑完电池/像素门禁，可以直接复制：

```bash
cp frontend/test-results/report.json reports/artifacts/playwright.json
```

注意最后一次 Playwright 运行会覆盖该文件；要一份同时含电池和像素的报告，就在一次运行里同时打开 `CQ_E2E=1 CQ_VISUAL=1`。

## 3. 四种方块状态

| 方块 | 含义 |
|---|---|
| 绿 | 通过 |
| 红 | 失败（Playwright `flaky` 也标红，并在详情注明“重试后通过”） |
| 灰 | 合法跳过（JUnit `skipped` / Playwright `skipped`） |
| 空心虚线 | 未运行：该层没有结果文件（每缺一层计 1 个占位块），或探索 manifest 里显式 `not_run` 的探针 |

悬停显示测试名；点击打开详情面板：测试名、所在层、`文件::用例`、状态、耗时、失败信息。所有来自结果文件的文本在进入 HTML 属性或 JSON 前都会转义；详情面板用 `textContent` 写入，不拼接 HTML。

## 4. 探索层 manifest

探索 agent 把每轮探测点写到 `reports/artifacts/exploration.json`。格式：

```json
{
  "run_label": "exploration 2026-10-25",
  "probes": [
    {
      "name": "双击领取按钮（并发点击）",
      "status": "passed",
      "duration_ms": 1840,
      "file": "exploration/claim_race.py",
      "case": "double_click_claim",
      "detail": "服务端一次成功、一次业务冲突"
    },
    {
      "name": "过期重置链接重放",
      "status": "failed",
      "file": "exploration/auth_replay.py",
      "case": "expired_reset_link",
      "message": "过期链接返回 200 并重置了密码（预期 400）"
    }
  ]
}
```

字段：

- `name`（必填）：方块悬停/详情显示的名字。
- `status`（必填）：`passed` / `failed` / `skipped` / `not_run`；其他值会让生成器 fail-closed（退出 1）。
- `file`、`case`（可选）：拼成 `文件::用例`。
- `duration_ms`（可选，非负数）：耗时。
- `message`（可选）：失败信息；没有时退回 `failure` / `detail`。
- `detail`（可选）：人读说明（适合 skipped / not_run）。
- 顶层 `run_label`（可选）：备注用途。

可运行示例：`docs/operations/test-report-exploration.example.json`（生成器单测会解析它，防止文档示例漂移）。

## 5. 已知限制

- **前端单测定位**：Node 22 的 junit reporter 只输出 `classname="test"`，不带源文件路径；第 4 层的 `文件::用例` 显示为 `test::用例名`。后端 pytest 的 JUnit XML 会推导出 `tests/.../*.py::用例`。
- **Playwright visual 分流**：按 spec 文件名是否包含 `visual` 判断（含 `visual-regression.spec.ts`、`*-visual.spec.ts`、`visual-capture.spec.ts`）。新增像素 spec 请沿用该命名。
- **离线安全**：HTML 不引 CDN、外部字体或外部 JS；测试数据里出现的 URL/尖括号只是被转义后的文本，不会成为资源请求。
- **响应式**：1440px 与手机宽度都不横向溢出；方块自动换行，详情面板在窄屏改为单列。
