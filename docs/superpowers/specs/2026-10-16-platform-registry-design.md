# Platform Registry Design（spec §7 增补）

> Status: DRAFT — awaiting owner review
> Date: 2026-10-16 · Amends: 2026-09-19-campusquest-design.md §7（assignment.platform 封闭枚举 → 开放注册表）
> Origin: owner 生产首发招聘采集任务（task-baseline-recruitment-2026-10.md），方案 B 占位约定的枚举扩展收口

## 0. 决策

assignment.platform 从 DB CHECK 封闭枚举（xiaohongshu|douyin|zhihu）改为**平台注册表**（表驱动）：

| 备选 | 结论 |
|---|---|
| 枚举继续加值+每次迁移 | 否——owner 明示"未来肯定更多平台"，每次开发介入违背"管理员面板自助"原则 |
| **平台注册表（本设计）** | 是——加平台=管理台操作，零迁移零发布 |
| system_settings JSON 白名单 | 否——无结构（显示名/排序/启停）、无 FK 强一致 |

## 1. 数据模型

新表 `platforms`：
- `code`（PK，ASCII 稳定码：`boss-zhipin`/`zhaopin`/`58city`/`lietou`…，创建后不可改）
- `display_name`（中文显示名：BOSS直聘/智联招聘/58同城/猎聘…，可改）
- `sort_order` int（学生端选择列表排序，可改）
- `enabled` bool（false=不可用于**新**任务；存量 assignment 引用不受影响）
- `created_at`

`assignment.platform`：**去除 CHECK，改 FK → platforms.code**。平台有 assignment 引用时不可删除（管理台只提供启停，不提供删除——简化生命周期）。

种子迁移：xiaohongshu（小红书）/douyin（抖音）/zhihu（知乎）/boss-zhipin/zhhaopin 修正为 zhaopin（智联招聘）/58city（58同城）/lietou（猎聘）。

## 2. 存量占位回填（同一迁移）

生产 20 行 platform=zhihu 占位按 keyword 前缀回填真实 code：
`BOSS直聘%→boss-zhipin；智联招聘%→zhaopin；58同城%→58city；猎聘%→lietou`。
**keyword 一律不动**（已发布内容的一部分，学生端可能已见）。回填后学生端显示真实平台名，方案 B 占位结束。

## 3. API 面

- `GET /api/v1/platforms`：登录后只读字典（code/display_name/sort_order；仅 enabled），前端领取按钮与任务展示消费 display_name。
- 管理台 CRUD（AdminActor+审计行）：
  - `GET/POST /api/v1/admin/platforms`（POST 创建：code+display_name+sort_order）
  - `PATCH /api/v1/admin/platforms/{code}`（display_name/sort_order/enabled；code 不可改）
  - 审计动作 `PLATFORM_CREATED`/`PLATFORM_UPDATED`（flush-only 模式）。
- 导入校验（import_parsing.py:64-66）：封闭集合→注册表查询；未知平台=行级错误进预览。
- OpenAPI 快照+TS 类型同 PR 再生。

## 4. 前端

- `ClaimButton.tsx:147` 等直渲 claim.platform 原码处 → 渲染注册表 display_name（平台字典经 /platforms 拉取+缓存）。
- 管理台新增「平台管理」页（列表/新增/启停/改名/排序；模式复用奖励目录页）。

## 5. 测试与工件（G19）

- 迁移 up/down 双向；种子+回填幂等断言（本地栈演示世界含旧枚举行）。
- 注册表 CRUD 权限矩阵（TEACHER/STUDENT 403）；FK 保护（有引用不可删——管理台无删入口，DB 层 FK 测试钉）。
- 导入未知平台行级预览错误；enabled=false 平台不可用于新任务。
- e2e：学生端领取按钮显示 display_name；管理台平台 CRUD 全链。
- 矩阵行+selector 契约+像素基线（新管理页）照走。

## 6. 非目标

不改 keyword 语义；不做平台删除；不做每平台独立 schema 模板（未来可议）。
