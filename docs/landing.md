# 智汇华师 Landing

Q14已确认三屏设计，Q15批准访客首页介绍、已登录首页工作台。`/about`始终公开。原(student)受保护页面继续按原规则要求登录。

运行路径：Next根页读取现有csrf/cq_shell会话证据决定首屏；useSession仍决定真实身份、错误和角色跳转。过期证据最终回Landing。根页之外不改变认证、注册、API或授权规则。登录页和登录成功落点沿用原系统；首屏和最终印面都导航顶层/login，不再显示演示反馈。

资源：`frontend/public/landing`为完整自包含原生文档，通过有标题同源iframe隔离全局CSS、Canvas、history与生命周期。81项运行资源及SHA256在runtime-manifest.json，输入为Q14确认ZIP（SHA25689ed3db69494f436d814c6fd77e986c568f0849073730754f1076d39e82311f3）；许可证同目录保留。正式版本只改变登录接线、文案和故障恢复，不改Q11两倍慢退、手机中轴、标题或印面。诊断控件保留为hidden/inert内部状态，不提供面向访客的工具面板。

部署：按现有Next前端构建和托管。CQ_BASE_PATH在构建时设置（例如/campus），Next配置显式暴露相同前缀给iframe；原生相对模块URL再推导带前缀/login。API仍在现有同源/api/v1，按原代理契约处理。无需新增后端服务、数据库或安装动画库。外部反向代理须允许同源frame和正确JS MIME，不应把缺失静态脚本伪装为首页。

维护：原生脚本/样式的可维护真源就是public/landing中的第一方文件；vendor与dist为固定第三方成果，勿盲目升级。修改资源后同步runtime-manifest的bytes/sha256，保留来源与变更说明；完整性单测会拒绝不一致。23份可迁移的动效/状态机测试保留在frontend/landing-tests，直接测试实际生产模块，不依赖原工作区、Windows字体或临时QA。

验证命令：使用CI相同的Node22。`npm run typecheck`、`npm run lint`、`npm run check:css`、`npm run arch:lint`、`npm run test:unit`（应用870项加动效110项）、`npm run coverage:ratchet`、`npm run test:e2e:landing`。最后一项先生产构建，再通过独立49356端口跑真实Next/public组合，无需共用DB/MinIO；会话角色与错误响应在该专用套件明确控制，不能冒充真实后端登录验证。既有工作台/登录电池保留。CI分别测试根部署与/campus。

本次已验证：应用870项与动效110项、覆盖率95.38/92.25/90.70、构建、根路径完整11例、前缀完整10例以及补充真实桌面动效到DONE；后续最终结果以当前head日志和CI为准。移动axe和Windows新稳定截图已检查。两张新Linux基线从fork运行41973717483的产物13390178651取得（ZIP校验通过），已人工对照Windows与批准构图；仅新增Landing基线，不复写旧工作台baseline。首次Linux9/11的两失败均为缺新基线，补入后的比较结果另记。原生305px印面高DPR柔化、实际后台/BFCache/物理高刷和外部正式部署仍不声称本轮验证。

当前账号对上游仓库没有直接写权限，因此发布为现有fork的分支，再向Penghuige/campus-quest main提交PR；推送/PR不代表owner已合并或网站已部署。
