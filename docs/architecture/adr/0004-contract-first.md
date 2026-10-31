# ADR 0004: 契约优先——OpenAPI 快照与生成的类型是前后端的单一事实源

日期: 2026-10-31 · 状态: 已接受
关联: backend/scripts/dump_openapi.py · frontend/openapi.snapshot.json ·
frontend/src/lib/api/schema.d.ts（npm run api:types:snapshot）·
Makefile 契约 freshness 段 · AGENTS.md「Test artifacts manifest」

## 决策

后端接口的**传输契约是生成物，不是手写物**，且它的新鲜度是被门禁
盯住的：

1. 后端路由的 Pydantic 模型 + **docstring**（docstring 进 OpenAPI 的
   description 层）共同构成唯一契约源；`dump_openapi.py` 生成
   `frontend/openapi.snapshot.json` 快照入库。
2. 前端 API 类型由快照生成（`npm run api:types:snapshot` →
   `src/lib/api/schema.d.ts`），前端不手写接口形状——两端对"字段叫什么、
   可空不可空"没有第二种理解。
3. **freshness 是合并门禁**：任何路由/模型/docstring 变更必须与再生成的
   快照和类型**同一个 PR** 落地；门禁用 `git diff --exit-code` 比对
   工件，漂移即红。历史上两次事故（#71→#75 与 #76）都是 docstring 改了
   工件没跟——这条门禁就是从这两次学费里长出来的。
4. 隐私规则随契约一起机器化：对外 DTO 显式构造（不直接序列化 ORM 对象），
   对象 key、内部 ID、手机号/邮箱等不入公共 DTO（spec §40），配合
   传输层测试钉住。

## 理由

- 契约漂移是前后端协作中最贵的一类静默 bug：类型对不上通常在联调
  深处才炸。把契约变成两个仓库内可 diff 的生成文件后，漂移退化成
  一个 `git diff` 就能看见的问题。
- docstring 计入契约把"文档"从可能撒谎的注释变成会挡合并的工件——
  写了不看门禁就过不去，比"请记得更新文档"可靠一个数量级。

## 被否决的替代

- **手写 TS 接口/手维护 swagger**：两份手写物必然漂移（且已经漂移过）。
- **运行时才校验（前端宽松解析）**：把契约错误推迟到用户面前。

## 后果

- 改 API 面 = 改模型/docstring → 跑再生命令 → 快照+类型+代码同一 PR
  （G19 清单在 AGENTS.md 常驻）。
- 再生成前必须基于最新 main（快照会静默倒退他人在 main 上的 API 面，
  事故在案），CI 之外本地跑再生是纪律动作。
