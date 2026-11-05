# Landing 真实接入设计

用户已批准Q14完整视觉，2026-11-05明确要求实现为可实际使用并推到Penghuige/campus-quest；Q15明确批准访客首页Landing、已登录用户原工作台，另有公开介绍地址。复用现有视觉与inline执行方式，不再重做设计审批。

基线origin/main@4876594cc8943295d97a29423541e0b7c386e1f0；隔离分支codex/landing-production。原脏工作区不动。批准资源来自zhihui-huashi-workbuddy-v2-2026-11-04（80项本地运行文件）。

根页移出(student)路由组，无csrf和有效cq_shell证据时直接SSR Landing。有证据复用原StudentShell与Dashboard；cookie不充当身份授权，useSession仍为权威，根页若解析为anonymous退回Landing，error保持重试、staff保持角色重定向。其他受保护路由及登录后落点不变。/about对所有人公开。

原生三屏运行文档放public/landing，以有标题的同源全屏iframe隔离全局CSS、Canvas、history和生命周期；React不重写动效。iframe src与登录入口必须支持CQ_BASE_PATH。两处登录均导航顶层窗口的同源/login，清理演示语义，并给禁用脚本/资源失效提供真实登录链接。使用现有账号体系，不新增认证后端、不假成功。

保留Q11两倍慢退、手机静态中轴、天空宋体、Q9-Q12新印面、三条用途句无句号、SCROLL增强/隐藏。所有库和字体本地，自包含资源清单记录SHA256与来源，保留许可证。不得引入来源路径或调试面板到面向用户流程。

测试包括真实Next路由/资源组合、两入口顶层导航、过期cookie/服务失败/角色隔离、basePath、桌面/手机/reduced/无GL、键盘/axe与新稳定屏幕像素证据。前端静态/架构/单测/覆盖/构建与仓库pre-push；新规约矩阵与选择器同PR。不得用旧143项数字冒称当前主站通过。推送独立分支、开PR供仓库owner合并；不自行合并或声称已上线。
