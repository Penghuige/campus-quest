# Test Report Dashboard — 实施计划

> 任务交接：`dashboard-2026-10-25`。设计稿已由 owner 确认（洛谷风格：每层一行、每个测试一个方块、点击看详情）。本计划只覆盖交付实现与验证，不改产品行为。

**Goal:** 增加 `make test-report`，把 pytest JUnit XML、Playwright JSON、Node `node:test` JUnit XML 与探索层 manifest 聚合成一个离线可用的 `reports/test-report.html`。固定七层：后端单测+worker、后端集成、后端全栈 e2e、前端单测、Playwright 电池、像素回归、探索层。

**Architecture:** 纯标准库 Python 生成器 `backend/scripts/test_report.py`：adapters（JUnit / Playwright JSON / manifest）→ 领域模型（TestCase / LayerReport / Report）→ 静态 HTML 渲染。Makefile 只负责“缺快套件时补跑”，需要 PG/浏览器的层一律不自动跑、不借栈。前端不新增依赖；生成物 `reports/` 全部 gitignore。

**Tech Stack:** Python 3.12 标准库（argparse / dataclasses / json / xml.etree / html）；Make；Node 22 `node:test` 内置 junit reporter；Playwright 内置 JSON reporter。

## Artifact Contract

| 层 | 结果文件（`reports/artifacts/`） | 谁产生 |
|---|---|---|
| 1 后端单测 + worker | `backend-unit.xml` | `make test-report` 缺失时自动跑；也可手动 |
| 2 后端集成 | `backend-integration.xml` | 手动（需要真实 PG） |
| 3 后端全栈 e2e | `backend-e2e.xml` | 手动（需要完整栈） |
| 4 前端单测 | `frontend-unit.xml` | `make test-report` 在 `frontend/node_modules` 存在且文件缺失时自动跑 |
| 5 Playwright 电池 | `playwright.json` | 手动（需要完整栈） |
| 6 像素回归 | `playwright.json` | 同文件；spec 文件名含 `visual` 的用例自动归入本层 |
| 7 探索层 | `exploration.json` | 探索 agent 按 manifest 格式写入 |

状态映射：JUnit `failure`/`error` → 红；`skipped` → 灰；其余 → 绿。Playwright `unexpected` → 红，`skipped` → 灰，`expected` → 绿，`flaky` → 红并注明“重试后通过”。结果文件缺失 → 该层一个虚线“未运行”占位块（每层计 1 个未运行）。

## Tasks

- [ ] **Task 1 — 失败测试先行** `backend/tests/unit/scripts/test_test_report.py`
  - JUnit（pytest 风格 + Node 风格）状态/耗时/定位解析；
  - Playwright 嵌套 suite、project 后缀、flaky、全局 errors、visual 文件分流；
  - manifest 四种状态与非法状态 fail-closed；
  - 缺文件 → 七层占位、计数；
  - HTML 转义（测试名/失败信息含 `<script>`、`<img onerror>`）、七层标题与计数区、无外部资源；
  - CLI 成功/失败退出码；示例 manifest 可被解析。
- [ ] **Task 2 — 生成器** `backend/scripts/test_report.py`
  - adapters + 固定七层 + 离线 HTML（内联 CSS/JS，数据经 `textContent` 写入详情面板）；
  - 所有数据文本进入属性前 `html.escape`；嵌入 JSON 把 `<`/`>`/`&` 转成 `\uXXXX`；
  - 1440px 与窄屏都不横向溢出（grid 可收缩、`overflow-wrap`、方块换行）；
  - 退出码：报告写出 0；解析/写入失败非 0。
- [ ] **Task 3 — Make 入口** `Makefile`
  - `test-report`：建目录 → 缺后端 unit+worker XML 时补跑 → 缺前端 unit XML 且有 `node_modules` 时补跑（无则跳过并传 note）→ 调生成器；
  - 不自动跑集成/e2e/Playwright/像素；
  - 测试红但 XML 已写出时继续生成报告（报告会显示红块）；XML 未产出则 make 失败。
- [ ] **Task 4 — 文档**
  - `docs/operations/test-report.md`：命令、产物、四态、artifact 表、manifest 格式、刷新方式、退出码、限制；
  - `docs/operations/test-report-exploration.example.json` 示例；
  - `docs/quality/agent-tooling.md` §14 加一行指针。
- [ ] **Task 5 — 验证与提交**
  - 生成器单测全绿；ruff check / format --check / mypy 显式文件全绿；
  - fixture 生成 HTML 断言七层与方块；真跑 `make test-report` 并 grep 七层与计数区；
  - 小步英文 commit，无 AI trailer；不 push。

## Acceptance

1. `cd backend && uv run pytest tests/unit/scripts/test_test_report.py -q` → 全绿。
2. fixture 生成 HTML：文件存在，含七层标题、`总计`、预期方块状态。
3. `make test-report` 真跑：后端 unit+worker XML 产出；前端无 `node_modules` 时层 4 显示未运行并注明；生成 `reports/test-report.html`，grep 七层结构与计数区通过。
4. `cd backend && uv run ruff check <files> && uv run ruff format --check <files> && uv run mypy <files>` 全绿。
5. 报告给出提交 SHA、生成文件路径、每条验证命令与结果、未决项。

## Out of Scope

- 不新增 npm/Python 依赖；不自动 `npm ci`；不自动借栈；不改既有测试语义；不重拍像素基线；不 push/发 PR。
- 不做深色主题切换（默认浅色；设计稿允许默认浅色）。
- Node 22 junit reporter 不携带源文件路径：前端单测详情定位为 `classname::用例`，在文档中注明。
