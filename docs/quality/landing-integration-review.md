# Landing 接入评审与交付证据

范围：Q14批准画面与Q15首页分流。基线4876594cc8943295d97a29423541e0b7c386e1f0；只在codex/landing-production独立工作树实施。没有修改原脏checkout、封存v1或部署主站。

独立终审使用全新上下文，审查4876594..a9f81cb。结论Critical 0、Important 2、Minor 0。两项Important在一次修复过程中处理；没有以重复评审代替测试。

| 发现 | 复现与修复证据 |
|---|---|
| renderer或其依赖无法下载时，原模块内部catch无法执行，最终场景无入口 | 原构成页面拦截render.mjs后登录链接不可见RED；独立动态import catch与外置原生入口修复，进一步真实点击暴露父层pointer-events:none，显式修复链接后GREEN。另拦截Three核心依赖，验证焦点恢复和Enter顶层登录。正常画面与资源哈希一并回归 |
| 专用Landing测试被共享配置重复收集，cookie origin与共享端口不同 | 实际Playwright --list检查RED；共享Chromium明确排除该spec，专用配置独占。检查脚本进入专用CI，保留原共享电池 |

实施裁决（按作出顺序）：

1. Windows已有文件名大小写冲突使TypeScript失败：仅把两个纯辅助模块改名为*State.ts并修正引用，逻辑不变。代价：diff多出少量可移植性文件。
2. 原生运行时仍读取诊断控件值：保留hidden/inert内部DOM，关闭访客检查面板。代价：保留少量不可见DOM。
3. 已有CSS门禁依赖POSIX grep及Windows需特权symlink：用git argv和JS筛选，测试fixture在Windows用junction；保留4项原回归。代价：测试工具增加平台分支。
4. 桌面默认Node24的navigator.locks导致旧认证测试与Node22环境不同：按仓库CI Node22验证，没有改生产认证代码或减弱断言。后端mypy显式检查Linux生产平台。代价：本轮不宣称默认Windows/Node24全门禁通过。
5. 当前账号对上游直接push返回403：推到已有fork并向Penghuige/main创建PR156。代价：仍需owner审核合并，没有绕过ACL。
6. 需要真实Linux新屏幕证据：先以draft推送并临时在fork分支启用专用工作流，仅导入两张新基线，不重写旧图；获得证据后撤掉临时触发。代价：引导期间draft CI曾因缺基线红灯。
7. 评审未判断固定vendor内部或升级：保持批准分发物，只审查集成、完整性和运行行为。代价：固定依赖本身潜在问题仍需后续升级工作处理。
8. 评审未实测物理高刷、真实BFCache、外部部署和完整数据库：不把公开页面与受控API响应测试称为真实后端认证或上线验收。代价：这些环境仍需发布阶段验证。

无延后Minor。保留PR工作树供后续审阅；不会删除唯一的本地证据或用户改动。

## 当前验证状态

本地最终结果：Node22应用870/870、原生动效110/110；覆盖率95.38/92.25/90.70，高于全部floors。生产Next根路径与/campus各13/13，移动axe、正常DONE、两个新稳定屏幕比较、真实模块/依赖失败恢复均通过；类型、lint、CSS、前后端架构、map/matrix、Ruff、Linux mypy、ratchet方向、placeholder、契约新鲜度及收集隔离共18项检查通过。原有71项架构告警与4项CSS告警未增加。更新矩阵后再次检查30文件通过。

完整本地日志与显式exit保存在独立工作树.local-dev/landing-integration/，final-root-e2e.*、final-prefix-e2e.*及gates/final-fix-*。测试控制角色/故障API响应；不替代真实后端登录测试。

最终运行代码head为00fb8b2423256598996ea7e5c2e45d00c73d6f0a：[fork Linux run41975917214](https://github.com/YeZesen0121/campus-quest/actions/runs/41975917214)成功。root job127074619191与/campus job127074619416均收集隔离检查通过、原生110/110、生产构建与浏览器13/13，失败与跳过均0；两张新增Linux基线比较通过。随后收尾提交只更新本文档/计划并移除临时fork分支触发，所有frontend代码及基线与该通过head逐字相同。正式工作流保留main push与main PR触发。

上游同一代码head运行41975924137（Landing）及41975924113（CI）状态action_required，尚待维护者批准；最终PR head同样必须由上游按自己的完整门禁验证。因此保持draft，不把fork公开页面套件等同于完整Backend/Frontend/Pixel绿灯。新基线来源与校验见landing-visual-baselines.md。

上游Actions对fork贡献需要维护者批准，fork专用运行不等于上游Backend、Frontend和Pixel完整门禁已绿。没有执行合并或部署。
