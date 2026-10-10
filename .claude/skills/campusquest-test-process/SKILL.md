---
name: campusquest-test-process
description: CampusQuest 测试流程契约：改动类型→门禁矩阵、测试工件清单、规约对账矩阵方法论、端口借用与基线纪律。写测试、加特性、动测试基建或准备 PR 证据时使用；不适用于纯文档改动与生产部署操作。
---

# CampusQuest 测试流程

## 改动类型 → 必跑门禁矩阵

| 改动类型 | 静态 | 单测 | 集成 | e2e 电池 | 像素 | 其他 |
|---|---|---|---|---|---|---|
| 后端 app/ 逻辑 | ruff format/check + mypy | ✔ | ✔（真实 PG） | 涉及 e2e 覆盖流时 | — | 触及不变量→并发测试（G15）；改 OpenAPI 面→dump 生成物同 PR |
| 前端 src/ 逻辑 | typecheck + lint + check:css | ✔ | — | 涉及交互语义时 | — | — |
| 前端视觉/CSS/组件 | 同上 | ✔ | — | A 类语义面 | ✔（不重生成基线；例外见下） | 基线重拍仅限字体归一化类且独立 commit + 走查记录 |
| 测试基建自身 | 同所属树 | ✔ | ✔ | 全量 | 全量 | 棘轮/audit/no-skip 自证一轮 |
| 契约工件（schema/snapshot） | 双树全件套 | ✔ | ✔ | ✔ | ✔ | 证据纪律第 4 条：跨树文件跑跨树门禁 |

门禁命令纪律：裸跑 + 显式成功标记；数字出自当轮日志（agent-tooling §13）。

## 测试工件清单（G19，特性 PR 同 ship）

见 AGENTS.md "Test artifacts manifest" 节。速查：新行为→矩阵行；会合法 skip 的测试→no-skip 豁免；稳定新界面→像素基线；spec 依赖的新选择器→契约 B 表；覆盖变化→棘轮地板走 `--update`；API 面变化→OpenAPI 快照+生成类型。

## 规约-测试对账矩阵方法论

模板：`docs/quality/test-matrix/points-ledger.md`。纪律：**预期列先于测试检索从 spec 原文推导落笔**；找不到测试=缺口（列出，不补）；测试行为≠预期=不匹配（上报，不擅改——bug 或 spec 缺口由 review 裁定）；裁决类取舍在行内注记。每域一文件；checker：`backend/scripts/check_test_matrix.py`。

## 本地栈借用协议（3000/8100 + 测试库）

e2e 电池与像素回归必须用 3000（MinIO CORS 白名单唯一端口，3002 已实证不通）。流程：向 campus reviewer 会话通告"借 3000"→ 其停常驻栈（owner 的 3000 环境）→ 跑电池 → 通告"还" → 其重启栈。**占用 campusquest_test@15432 的任何测试跑——包括不经端口的后端直跑（pytest/覆盖率采集/flake 挖掘）——都视同借栈，同样通告-等待-使用**（2026-10-08 起生效：直跑测试与电池并发用库会互相污染证据）。

## 像素基线纪律

- 默认：**不重生成**——迁移/重构类改动的等价性由 byte-stable 证明
- 重生成仅限：字体归一化类（spec 内 addStyleTag 钉 sans+mono）或产品级视觉变更（owner 批准）
- 重生成必须：清库纪律（TRUNCATE 全表）+ 新旧基线走查（差异归类：逐像素一致/字体度量/内容——**内容差必须查明真因**）+ 独立 commit 记录
- CI 的 visual job 是第二环境证据（CI 配额暂停期：job 配置仍在但不产出证据，配额恢复后自动生效）；字体双端漂移=钉字体解决，禁止调宽容差过关（尺寸不匹配先于比率失败，且容忍度会让门禁失明）

## 覆盖率棘轮

前后端各有地板文件（frontend/coverage-ratchet.json、backend/coverage-ratchet.json）。降覆盖→同 PR 走 `--update`/`coverage:update` 给理由；地板以 CI 实测为准（权威环境；CI 配额暂停期以本地 coverage-ratchet 实测 + epsilon 为准，配额恢复后自动回落 CI），跨环境噪声由 epsilon 吸收（实测漂移 0.1pp → epsilon 0.15）。coverage-ratchet 已进入 `make release-gate` 依赖链（2026-10-10 owner 批准）。

覆盖漂移/flake 排查工具箱（PR #36 先例）：单模块漂移而其余分毫不差 = 单测分支覆盖 flake，非系统噪声。定位法——同一套件 N 轮独立 `COVERAGE_FILE` 运行 + coverage json 差分，找出偶走偶不走的分支；修法——确定性单测钉住该契约的全形态（不靠竞态运气拿覆盖），源码不动。
