# F01–F07 修复：本轮验证回执

2026-10-12，分支 `codex/innovation-platform`。用户在深度验收后回复“修复”，授权修复 [验收报告](innovation-user-acceptance-2026-10-12.md) 的 F01–F07；不开始 R4，不更改成果复审、招募资格或学校认证的已确认规则。

## 被验源码与证据身份

- 起点：`c4e2e043bc88a4f678a490ba7ba3c6b773d581f4`。
- 最终产品、单元与浏览器测试源码：`457f09b9ddbd55685190293344e1f62e304b9e05`。
- 招募三张批准基线及走查文档：`4aabf368c2763f220fa986f301e068ef61feb603`。这一步没有再次改产品或测试逻辑。
- 最终浏览器与像素执行从此时冻结的 977 文件 source tar 解包；SHA256 清单位于工作区 `.local-dev/logs/qa12-source-manifest.json`。文本文档归一 CRLF，PNG 保持原始字节；不以文档收尾提交冒充代码被重新测试。
- 所有完整输出均位于仓库外 `C:/Users/111/Desktop/华师令/.local-dev/logs/`，保留明确退出码。最终 Git/CI 对账另存 `qa12-final-git-ci.json`。

## 门禁结果

| 验证 | 本轮结果 | 完整输出 |
| --- | --- | --- |
| Windows 前端八门禁 | typecheck、lint、CSS、unit、coverage、ratchet、audit、build 均退出0；670 单测通过 | qa12-windows-frontend.log；完整转码副本 qa12-windows-frontend.utf8.log |
| Linux 前端八门禁 | 相同八项均退出0；670 单测通过 | qa12-linux-frontend-final.log |
| 覆盖率 | coverage报告lines 95.57%、branches 91.67%、functions 90.22%；ratchet按其精度报告Windows lines 95.56%、Linux 95.57%，均高于原地板95.21/91.22/90.19 | 两端 coverage/ratchet 完整输出 |
| 本批定点浏览器 | 9 通过；真实后端/PG、未知结果回执、逐字纠错均覆盖 | qa12-review-acceptance-green.log |
| 定点恢复/未知命令单测 | 6 通过 | qa12-review-recovery-green.log |
| 全站基础像素 | 16 通过，独立 seed/teardown，未更新基线 | qa12-final-visual-regression.spec.ts.log |
| R1 / R2 / R3 像素 | 分别1 / 1 / 2通过，分别独立 seed/teardown，不带 update-snapshots | qa12-final-innovation-navigation-visual.spec.ts.log；qa12-final-innovation-review-visual.spec.ts.log；qa12-final-innovation-recruitment-visual.spec.ts.log |
| 全站浏览器 / no-skip | 170收集、139通过、31具名条件豁免；0 unexpected / 0 flaky，三浏览器 retries=0，errors=[]；29 spec均在报告，no-skip退出0 | qa12-final-battery.log；qa12-final-noskip.log；qa12-final-battery-report.json |
| 招募规约矩阵 / git diff --check | 当前检查退出0，收尾文档后再核验 | docs/quality/test-matrix/innovation-recruitment.md |
| 独立评审 | 首轮2项 Important 接受、复现并修复；最终457f定点复审 APPROVE，无新 Critical/Important/Minor | innovation-acceptance-fixes-review.md |

Node 22.23.3，两端实际 Next 16.4.0。Audit 通过既有5个具名 HIGH 开发依赖链豁免（reviewBy 2027-01-07），不是零漏洞；本批未新增依赖或放宽门禁。Backend 源码、API、数据库迁移及 worker 未改变，不把历史后端全套通过数当作本批重新执行结果。

自动浏览器环境为WSL CampusQuest-Dev下本批容器 `cq-ie-acceptance-fix-20261012`，复制运行目录 `/qa-fix`；测试Web HTTPS3006/API8400，数据库 `campusquest_test_ie_acceptance_20261012`、Redis DB12、独立MinIO compose项目 `cq-ie-acceptance-20261012`（TLS9002）。数据库沿用现有迁移，测试setup/teardown仅处理本批测试世界；开发Web3000/API8200及开发库未重置。四组视觉分别seed/teardown后再跑完整全站世界。测试运行目录的git索引只为CSS检查，不是宿主仓库的Git变更。

浏览器总时长14.3分钟；招募spec26项通过（含本批9项），成果跨三账号每次更新审核用例通过。31豁免沿用既有清单：20视觉比较本轮已另跑通过，另4截图采集、3认证探针以及4特定seed/故障模式；不能把剩余条件模式说成这次已执行。既有axe基线问题未新增/放宽，不宣称全站WCAG完全合规。

最终源码身份核验 `qa12-final-source-check.json`：当前工作区与测试容器829个前后端/基础设施/CI输入文件均与冻结SHA256一致，mismatches=[]；backend/infra/CI相对起点diff=[]。只改交付文档不要求重跑未变化测试输入；原Windows报告解析采用显式UTF-8，默认GBK解码失败是证据读取问题，修正读取后stats核验退出0。

## 逐项符合性与用户旅程复验

| 问题 | 实现与新验证 | 实际开发浏览器复验 |
| --- | --- | --- |
| F01 站内离开丢输入 | 同标签、同认证 epoch 的项目隔离内存恢复；未自动保存/公开。9项中的 recovery、delayed-original-save；单测认证转换和旧epoch写入。迟到原保存只清自己拥有的快照，不删除后来新输入 | 新表单输入岗位/非法QQ→侧栏创新创业→浏览器返回，输入恢复且有恢复说明；显式丢弃后回列表。刷新/关闭提醒保留，不承诺跨刷新恢复 |
| F02 管理404误称服务故障 | 管理入口局部中性“不存在或不可访问”；无重试，有返回本人项目；未更改全局错误映射 | 不存在项目管理入口不显示私有字段或重试，点击返回我的项目成功 |
| F03 非适用创意字段 | 其他阶段编辑/预览省略目标和方案，切回恢复；超长旧输入仍可纠错，控件保持至保存/显式阶段切换 | 其他阶段编辑无目标/方案；阶段来回输入保留；重新开放预览不列不适用字段 |
| F04 联系格式纠错 | 四渠道格式与原验证器一致；QQ数字输入模式、邮箱/手机语义；非法值按渠道错误，缺选择才用组错误 | QQ abc 保存被拦、聚焦QQ、说明5–20位数字且非0开头，没有重复通用错误；390px手机无水平溢出，Tab能到下一授权控件 |
| F05 焦点/成功反馈 | 进入新建/编辑聚焦标题；本地返回焦点回对应创建/编辑按钮；常驻status只公告读取结果，不含联系值 | 实际键盘Enter进入/返回，焦点位置正确；主动读取后有“联系方式已读取”，仅单独展示授权模拟邮箱 |
| F06 零基础表达 | false 公开列表/详情均“不接受零基础”；true仍零基础友好 | 开放模拟岗位详情确认为“不接受零基础”，不转换成有能力要求 |
| F07 关闭提醒 | 仅OPEN/NORMAL/无截止日期且有next_remind_at展示下次提醒；不更改调度逻辑 | 实际确认关闭后“当前已关闭”，无下次提醒 |

实际浏览器仍使用原开发 Web3000/API8200，预置负责人20269001。只操作上轮新建的明确模拟项目 `3898b74b-7753-49a2-8000-d1673a7901d5`、岗位 `eb3cfe08-2cde-41ed-99df-1e8b54d19a90`；曾显式重新开放用于公开/联系验收，末尾正常关闭，记录保留。没有重置开发数据或修改其他业务记录。临时新表单丢弃，无服务器自动保存。测试邮箱 `qa-campus@example.invalid`，不是个人联系信息。

截图目录 `.local-dev/logs/qa12-user-fixes/`：qq-format-desktop.png、qq-format-mobile.png、public-contact-success.png、workspace-404.png。截图与实际 DOM/焦点观察共同作证；单张截图不能证明导航前后、未发请求或未泄漏。浏览器恢复默认viewport并留在创新创业首页。

## RED、修复与无效实验的区分

- 初始行为RED：qa12-browser-red-v2.log（6项）、qa12-contact-closure-red.log（联系反馈/关闭提醒）、qa12-unit-red.log（格式文案）。
- 评审发现先RED：qa12-review-recovery-red.log、qa12-review-delayed-red-v2.log、qa12-review-typing-red-v2.log；后定点GREEN并重跑两端门禁。
- 测试环境错误不算产品RED：物理node_modules替换越界symlink；task-done补显式tsconfig；Linux helper转LF、复制运行目录建立git索引用于CSS检查；占用8400的并发调用已撤回。
- 首轮延迟重试测试的成功状态应是现有幂等200，纠正测试期待后才出现真实RED，未改后端语义。
- 33ff的旧全站执行为修复评审问题主动中止，exit130，不能称完整通过。
- 首次合并四视觉spec共享账号资料，产生R2初始version409和负责人初值不符；最终拆为各自seed/teardown。只批准更新F04/F05实际影响的三张R3 PNG，未改R1/R2/基础基线、字体、容差。见 [截图走查](innovation-acceptance-fixes-visual.md)。

## 边界

已验证键盘、DOM关联、live status及自动axe；未实际运行屏幕阅读器，不能宣称读屏验收通过。导航恢复只保证同标签当前会话，刷新/关闭与跨设备持久草稿须显式保存。后续视觉布局整理、筛选上下文、存量模拟正文等建议仍未纳入本批。

本地门禁、独立评审和远端完整CI是三份不同证据。首交文档b9b1166保存推送后，local/fork/PR head一致、工作树干净；PR #43仍OPEN/Draft、CONFLICTING/DIRTY；GitGuardian单项进行中，两仓该SHA的Actions workflow run为0，没有完整CI通过证据。上游Actions/fork status的TLS握手超时后只读重试成功，不能称为CI失败。最终仅文档收尾提交的精确查询见 `qa12-final-git-ci.json`；不将首交SHA的检查移用为最终SHA的通过证明。

## 执行裁定与成本

- 选择同标签内存恢复，不修改路由history或替每个导航Link加拦截：跨站内路径一致、不持久化联系值；成本是刷新/关闭后只能依赖显式保存与离开提醒，不能承诺跨刷新恢复。
- 回执仅清其输入快照，纠错展示状态独立于错误文案：防止新输入被迟到命令删除、按键后控件卸载；若对象身份/生命周期改变，需重验延迟回执和逐字输入测试。
- 四视觉场景各自seed/teardown：避免同演示账号的资格/version写入互相污染；成本是启动四次服务与测试世界，执行时间增加；不放宽实际产品权限。
- 评审未覆盖的最终像素/全站回归由作者运行；真实读屏未执行，保留明确验收边界；R4和后端全域不扩展，不合并。若要正式无障碍结论或上线，需另补实际读屏与精确完整CI证据。
