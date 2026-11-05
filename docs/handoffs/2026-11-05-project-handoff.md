# 智汇华师（CampusQuest）项目交接说明

> 整理日期：2026-11-05（北京时间）。面向接手的学姐、仓库 owner 和后续开发者。
> 本文按后续可独立维护整理。具体人员分工、账户权限和正式移交时间由双方确认，不默认交接方继续承担后续开发。

## 1. 先看结论

项目已有校园任务与创新创业代码；本次最新交付是**已定稿 Landing 的真实前端接入**。Landing 已推送，但尚未合并到主仓库或部署到公网。PR 是代码审阅入口，不是体验网址；旧 ZIP 也不是最终接入版本。

| 项目 | 当前事实 |
|---|---|
| 正式主仓库 | [Penghuige/campus-quest](https://github.com/Penghuige/campus-quest)；main 是后续集成与发布真源 |
| Landing 交付入口 | [PR #156](https://github.com/Penghuige/campus-quest/pull/156)，2026-11-05核对为 open / draft / 未合并 |
| 完整代码 | [fork 的 codex/landing-production 分支](https://github.com/YeZesen0121/campus-quest/tree/codex/landing-production) |
| Landing 交付基准 | 8695d3d61518caecfab514dbb179d8d3a9df3609；本交接的后续文档提交不改变这批运行代码 |
| 本次 main 基线 | 4876594cc8943295d97a29423541e0b7c386e1f0，PR #155 合并后；接手时重新 fetch 核对 |
| 尚未完成 | 上游完整 CI、owner 审阅与合并、真实服务栈最终验收、外部部署 |

当前推送账号对上游直接 push 返回403，所以采用已有 fork → 上游 PR。学姐是否有仓库维护、服务器及域名权限尚未核实。

## 2. 怎么体验

### 现在这台交接电脑

本次已启动并检查：[打开本地 Landing](http://127.0.0.1:49356/campus/about)。仅在启动服务的电脑上有效，进程停止或电脑关机后失效，不能转作异地访问的公网链接。

可以体验：移动光标显影校园 → 底部箭头进入图文介绍 → 下一屏聚树成印 → 点击印面进入登录；首屏右上“直接进入”也进入登录。手机使用静态校园中轴构图，减少动态效果设置有静态回退。

当前只启动了前端。**打开登录页不等于完成真实账号登录**：实际认证需要完整 API、数据库及现有身份服务，本次预览没有启动这些后端依赖。

### 学姐在自己电脑复现（Windows / PowerShell）

准备 Node.js 22 和 Git，在新的空目录执行，避免覆盖其他开发现场：

~~~powershell
git clone --branch codex/landing-production --single-branch https://github.com/YeZesen0121/campus-quest.git campus-quest-landing
cd campus-quest-landing/frontend
node --version
npm.cmd ci
$env:CQ_BASE_PATH = '/campus'
npm.cmd run build
node node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port 49355
~~~

打开 http://127.0.0.1:49355/campus/about 。保持前台进程运行，Ctrl+C 停止；端口被占用就换一个空闲端口，不终止不明进程。复现命令选49355，避免占用专用测试端口49356。

仓库当前没有 npm start 脚本，以上直接调用 Next CLI。部署到根路径时，构建前清除 CQ_BASE_PATH 并重新构建，不能只改启动时环境变量。macOS/Linux 把 npm.cmd 换成 npm，构建和启动所在 shell 使用 export CQ_BASE_PATH=/campus。

## 3. 产品范围与已确认决定

校园任务部分沿用主仓库：教师发布任务、学生领取与提交、机器校验、教师审核、积分兑换、排行榜/成长、社区与通知等。见 [README](../../README.md) 与 [基础规格](../superpowers/specs/2026-09-19-campusquest-design.md)。规格中的历史上线标记，不代表本轮重新验收了现网。

创新创业模块已有项目、成果、招募、人才等路由与实现；相关四批次已通过历史 PR #142 合入 main，不是只存在旧 fork 的草稿。人才 R4.1 的材料标志/筛选也已有实现，不应沿用旧交接“尚未实现”的说法。材料标志是公开附件当前可读状态，不是真实性或能力认证；没有材料的学生仍可公开。见 [创新边界](../architecture/innovation-boundary.md)、[人才规格](../superpowers/specs/2026-10-12-innovation-talent-design.md) 和当前代码。

**Landing 已确认，不需要重新从头选择：**

- 名称“智汇华师”，不是“智慧华师”；天空留白处使用端正宋体标题。
- 用户提供的校园地标照片转为淡彩水墨；桌面光标显影后短暂保留，以原先约2倍的慢退节奏淡回纸面。
- 手机直接展示静态中轴，不要求看见完整校名石墙。
- 第二屏三行说明只删句末句号；SCROLL保留英文与循环竖线、适度增强，自动落印开始后隐藏。
- 最终印面 Log in 在上、四字在下，比例、字形、印色及质感已确认；右上无框“直接进入”暂按当前版保留。
- 访客访问首页看 Landing；已有会话继续原工作台，教师/管理员仍按原角色落点；/about 始终公开，其他受保护页面仍需登录。

学校 SSO、导师库、完整运营治理、后续激励等不能因出现在历史路线图中，就被当成本次已完成项或新排期。接手方再按当前规格、实际代码和 owner 决定核对，不附加新的功能承诺。

## 4. 代码与资料地图

| 处理什么 | 先看哪里 |
|---|---|
| 全项目启动、技术栈、目录 | [README](../../README.md)、[AGENTS.md](../../AGENTS.md) |
| 产品规则 | [基础规格](../superpowers/specs/2026-09-19-campusquest-design.md)；再读具体任务的规格/计划 |
| Landing 使用与部署 | [docs/landing.md](../landing.md) |
| 本次设计与计划 | [真实接入设计](../superpowers/specs/2026-11-05-landing-integration-design.md)、[实施计划](../superpowers/plans/2026-11-05-landing-integration.md) |
| 根页与会话分流 | frontend/src/app/page.tsx、frontend/src/app/about/page.tsx、frontend/src/app/(student)/_components/StudentShell.tsx |
| React 容器 | frontend/src/features/landing/LandingPage.tsx；同源 iframe 隔离全局CSS、Canvas、滚动和生命周期 |
| 三屏第一方运行真源 | frontend/public/landing/；不依赖交接电脑的临时素材目录 |
| 导航与动效 | landing/navigation.mjs、scene-host.mjs；hero/ 为显影，seal/ 为树与印面（均位于上述目录） |
| 资源完整性 | frontend/public/landing/runtime-manifest.json：81项运行资源的bytes/SHA256，修改资源须同步清单并保留许可证 |
| 浏览器测试 | frontend/e2e/landing.integration.spec.ts、frontend/playwright.landing.config.ts、.github/workflows/landing.yml |
| 动效状态机测试 | frontend/landing-tests/，直接测试生产模块 |
| 本次证据 | [接入评审](../quality/landing-integration-review.md)、[新基线来源](../quality/landing-visual-baselines.md)、[行为矩阵](../quality/test-matrix/landing.md) |
| 创新与共享层边界 | docs/architecture/，尤其 innovation-boundary.md |
| 整站验收 | [质量门禁](../quality/quality-gates.md)、[发布清单](../operations/release-checklist.md)、[测试报告说明](../operations/test-report.md) |

vendor/、dist/ 是固定分发物。不要为改标题或慢退重生成整套库，也不要重拍旧工作台基线强行通过。未来改动按 AGENTS.md 读取对应前后端质量文档、安装版本文档与测试规程。

## 5. 验证到什么程度

以下是具名代码版本的已有证据，不是接手时最新head的自动绿灯：

| 范围 | 已有结果与边界 |
|---|---|
| 本地 Node22 | 应用870项 + 原生动效110项通过；覆盖率95.38%行 / 92.25%分支 / 90.70%函数，高于原floors |
| 本地生产前端 | 根路径与/campus各13项通过，含移动axe、新屏幕比较、正常DONE、资源完整性和故障登录回退 |
| Linux 代码版本 | [fork run41975917214](https://github.com/YeZesen0121/campus-quest/actions/runs/41975917214)，head 00fb8b2423256598996ea7e5c2e45d00c73d6f0a，两种路径各13/13及动效110/110；8695d3d仅文档/CI触发清理，frontend逐字相同 |
| 静态质量 | 18项逻辑检查通过；原有71项架构、4项CSS告警未增加，详见接入评审 |
| 上游8695d3d | [完整CI](https://github.com/Penghuige/campus-quest/actions/runs/41976618186)、[Landing CI](https://github.com/Penghuige/campus-quest/actions/runs/41976618041)均为action_required，需维护者批准；新增文档提交后须核对新head |
| 本次未验证 | 完整数据库/真实后端登录、外部部署、真实BFCache、物理高刷新设备；公开套件角色/故障API是明确受控输入 |

独立终审的两项Important已先失败后修复通过：动效模块/依赖下载失败后的独立登录入口；专用测试与共享电池的收集隔离。没有延后Minor。这不等于精确最终head的owner APPROVE或上游完整CI已绿。

整理本交接时又核对了本地预览：介绍页、运行文档和登录页均HTTP200，真实页面的箭头与印面能跳到顶层登录页。交接的16个相对文档链接均存在，运行代码相对8695d3d无改动。文档推送前重新跑过18项逻辑质量检查、870+110单测，全部通过；本轮覆盖率95.40/92.25/90.70。当前Windows缺GNU make，make pre-push本身未能启动，上述为逐项等价命令的结果，完整日志保存在本地忽略目录.local-dev/landing-integration/gates/handoff-*，不能声称make命令通过。

Landing定点复核（frontend/，Node22）：

~~~powershell
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run check:css
npm.cmd run arch:lint
npm.cmd run test:unit
npm.cmd run coverage:ratchet
node scripts/check-landing-collection.mjs
npm.cmd run test:e2e:landing
~~~

最后一项自行构建并占用49356；本次交接电脑的手动预览也在49356，测试前先停掉自己启动的预览，不借用或停止不明服务。需要时 npx.cmd playwright install chromium；本机已有Chrome可设置 $env:CQ_LANDING_CHANNEL='chrome'。根路径与/campus需分别配置、分别构建。

## 6. 学姐接手的优先顺序

1. **确认人员与权限。** 分开确认GitHub维护者、服务器、域名和外部服务负责人。本轮没有生产访问资料，也没有转移账号。凭据通过团队既有安全渠道交接，不写入Git或本文。
2. **拉取PR最新代码并体验。** 按第2节先跑前端、确认三屏和两处入口；后续在独立分支开发。旧原型、旧ZIP和历史PR43不是最终交付入口。
3. **完成精确head门禁。** 维护者批准fork Actions；核对与main同步、未解决线程及该head的审阅结论。Backend (Python 3.12)、Frontend (Node 22)、Pixel visual regression (Playwright)三项必需绿灯及新增Landing工作流都要核对，后续文档提交也会改变head。
4. **启动完整应用并验收。** 按README与发布清单配置环境。make stack 启动Compose基础依赖和report profile报告查看器（本地8200），不是自动启动全部API/前端/Worker。还需Python3.12/uv、数据库迁移、API、前端、Celery Worker/Beat；人才附件需要真实ClamAV扫描。具体命令见下节。
5. **owner批准后合并与发布。** owner决定合并；合并后的main重新执行make verify、make release-gate，并按docs/qa/*.csv手动复现、记录结果。不得以旧run、fork公开套件或报告生成退出0替代验收，不绕过分支保护。
6. **补真实体验地址。** 本轮没有核实这版的公网部署地址。发布负责人记录域名、部署SHA、回滚方法及验收时间后，再向同学提供线上链接。

## 7. 整站启动与部署注意

完整启动以README、backend/.env.example、infra/env.example及发布清单为准，环境文件只参考键名，不能把示例密钥当生产凭据：

- PostgreSQL16、Redis7、MinIO由基础设施提供；人才附件扫描还需按运行手册启用ClamAV。
- 在backend/配置环境，uv sync，uv run alembic upgrade head；迁移走Alembic，不手工导入旧SQL或复用旧迁移编号。
- API示例：uv run uvicorn app.main:create_app --factory --port 8100。
- Worker/Beat入口为app.workers.celery_app:celery_app；按README启动，不能漏掉异步校验和调度。
- 前端开发代理可用CQ_DEV_API_PROXY=http://localhost:8100；部署须把同源/api/v1路由到真实API。仅启动前端时不会自动获得这个后端。
- CQ_BASE_PATH为构建时配置。挂/campus时页面与素材带前缀，API仍为同源/api/v1，不是/campus/api/v1。
- 保留Next Node服务和既有API架构。整个仓库不能作为纯静态站点发布后宣称完整可用；首页会话分流由Next服务参与。
- 反向代理需正确JS MIME、允许同源iframe、缺失静态资源返回404。部署后实际验证登录/刷新/退出及三角色落点。
- make test-report是报告生成器，退出0不代表门禁通过。不要为快速体验运行会清数据的clean-start或删除卷操作。
- 数据库备份/恢复、对象存储、短信/邮件、域名与密钥由负责人员核对。本轮没有访问或修改生产数据。

本轮没有改封存v1或原主工作区。原主工作区另有未提交包文件和历史草稿，未混入PR，这批Landing运行代码不依赖它们；其他历史资料是否另行归档，由接手负责人决定。R4/R4.1旧测试数字只作追溯，不作新发布验收。

## 8. 给下一位开发者或AI的启动提示

> 接手智汇华师CampusQuest。先读AGENTS.md、README、本交接、基础规格、相关计划和质量门禁§16；核实origin/main、PR #156、实际工作分支、未提交改动与精确head的CI。Landing运行交付基准8695d3d，已定视觉及Q15路由不重复问。先在独立目录用Node22启动前端；localhost不是公网地址，受控API测试不是真实后端认证验收。当前优先完成维护者工作流批准、精确head审阅/门禁、完整栈验收与部署，未经owner批准不合并或改生产。创新模块先读边界，不凭旧记忆判断是否实现。缺少权限或域名时列明确待移交项，不猜账号、网址或现网状态。

建议技能：修复前systematic-debugging、test-driven-development；测试/门禁先campusquest-test-process；已有批准计划后executing-plans；交付用verification-before-completion。新需求才用brainstorming、writing-plans，不重做已确认的Landing设计。
