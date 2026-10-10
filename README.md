# CampusQuest

高校任务协作平台——教师发布数据采集任务，学生领取并提交，机器自动校验，教师人工审核，积分入账（排行榜 / 钱包 / 兑换），配合社区互动与通知，形成完整的校园任务闭环。

```
教师发布任务 → 学生领取 → 上传提交（CSV / XLSX / SQLite / 文档）
    → 机器自动校验 → 教师审核 → 积分到账 → 兑换奖励
```

## 面向谁

| 角色 | 做什么 |
|---|---|
| **学生** | 浏览任务广场、领取任务、上传提交物、查看校验报告与审核结果、参与评论与评分、赚取积分兑换奖励 |
| **教师** | 创建与发布任务、批量导入任务单元、审核学生提交、管理任务生命周期 |
| **管理员** | 用户与白名单管理、奖励目录与兑换审核、审计日志、系统设置 |

适用于高校的数据采集类作业、校园调研、学习行为记录等场景——凡是"教师出题、学生交数据、系统自动校验"的任务型教学活动。

## 核心特性

- **任务闭环**：发布 → 领取（并发安全的名额分配）→ 提交 → 机器校验 → 人工审核 → 积分发放
- **两种任务形态**：结构化数据（CSV / XLSX / SQLite，按 schema 逐行校验）与文档（DOCX / PDF，完整性校验 + 教师审阅）
- **积分体系**：不可变台账（Ledger）、并发安全的钱包与库存、按截止时间阶梯的奖励档位、可追溯的冲销
- **排行榜**：日 / 月 / 总榜，Redis 投影可从 PostgreSQL 事实源随时重建
- **社区**：评论、投票、表情、任务评分、匿名发言（管理员可审计揭示）、举报处理
- **安全**：学号白名单注册 + 短信 OTP、员工 2FA（TOTP）、RBAC 权限矩阵、全链路审计日志、隐私字段不入 DTO

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | Next.js 16（App Router）· React 19 · TypeScript · Tailwind CSS v4 · Radix UI 原语 · Playwright（e2e + 像素回归） |
| 后端 | Python 3.12 · FastAPI · SQLAlchemy 2.x（异步）· Pydantic v2 |
| 任务队列 | Celery（文件校验、通知调度、超期释放、清理） |
| 数据 | PostgreSQL 16（事实源）· Redis 7（缓存 / 排行榜投影 / OTP）· MinIO（S3 兼容对象存储） |
| 基础设施 | Docker Compose · Caddy（HTTPS 反代）· Alembic 迁移 · GitHub Actions CI |
| 测试 | pytest（单元 / 真实 PostgreSQL 集成 / 并发不变量 / e2e）· 覆盖率棘轮 · 规约-测试对账矩阵 · axe 无障碍 · 跨浏览器 smoke |

## 架构

```
                    ┌────────────┐
        HTTPS ────▶ │   Caddy    │ ──── /campus 路径挂载
                    └─────┬──────┘
              ┌───────────┼───────────┐
              ▼           ▼           ▼
        ┌──────────┐ ┌──────────┐ ┌─────────┐
        │ Next.js  │ │  FastAPI │ │ Celery  │
        │ 前端 SSR │ │模块化单体│ │ Worker  │
        └──────────┘ └────┬─────┘ └────┬────┘
                          ▼            ▼
                  ┌────────────────────────────┐
                  │ PostgreSQL · Redis · MinIO │
                  └────────────────────────────┘
```

后端是模块化单体（按业务域组织：identity / tasks / submissions / points / community / notifications / audit），每个域内 `router → service → repository/port → adapter` 分层，事务边界在服务层。

## 快速开始

```bash
# 1. 基础设施（PostgreSQL / Redis / MinIO）
cd infra && docker compose up -d

# 2. 后端
cd backend && uv sync
cp .env.example .env
uv run alembic upgrade head
uv run python scripts/seed_demo_accounts.py
uv run python scripts/seed_demo_content.py
uv run uvicorn app.main:create_app --factory --port 8100

# 3. 前端
cd frontend && npm ci
CQ_DEV_API_PROXY=http://localhost:8100 npm run dev
```

打开 http://localhost:3000，用演示账号登录：

| 角色 | 账号 | 密码 |
|---|---|---|
| 学生 | `20250001` ~ `20250020` | `student-demo-2026` |
| 教师 | `teacher@campus.example.edu` | `admin-demo-2026` + TOTP |
| 管理员 | `admin@campus.example.edu` | `admin-demo-2026` + TOTP |

> 演示账号与数据仅用于测试，勿在演示环境放置真实数据。

## 测试

```bash
make test-backend     # 单元 + worker
make test-integration # 真实 PostgreSQL 集成（含并发不变量）
make frontend-unit    # 前端单元
CQ_E2E=1 make playwright-e2e   # 全量 e2e（需端口 3000/8100 空闲）
make verify           # 全部门禁
```

测试规约见 `docs/quality/quality-gates.md`；每个业务域维护"spec 规则 ↔ 测试"对账矩阵（`docs/quality/test-matrix/`）。

## 项目结构

```
backend/
  app/
    core/          # 配置、安全、时钟、错误码
    db/            # 引擎与会话
    integrations/  # 外部系统端口与适配器（S3、短信、邮件、限流）
    modules/       # 业务域（identity / tasks / submissions / points / community / ...）
    workers/       # Celery 任务
  alembic/         # 数据库迁移
  tests/           # unit / integration / workers / e2e / fakes
frontend/
  src/
    app/           # Next.js App Router 路由
    components/    # 共享组件（ui/ 为原语）
    features/      # 按业务域组织的前端模块
    lib/           # API 客户端、工具、生成的 OpenAPI 类型
  e2e/             # Playwright 测试
docs/
  quality/         # 质量标准与测试矩阵（工程纪律的权威来源）
  superpowers/     # 设计规格与实现计划
```

## 更多文档

- 设计规格：`docs/superpowers/specs/`（业务规则、状态机、API 契约的权威来源）
- 质量标准：`docs/quality/quality-gates.md`（含 Engineering Golden Rules）
- 前端设计系统：`docs/quality/frontend-design-system.md`
- 面向开发 agent 的入口：`AGENTS.md`

---

CampusQuest · 学术生产力产品，克制的游戏化设计。
