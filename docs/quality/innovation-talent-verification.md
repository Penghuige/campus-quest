# R4 人才库验证回执

2026-10-13。依据已确认的 R4 设计、五任务计划及人才规约矩阵。最终产品与测试修复保存于 `1e04425a6dc405ad41e88a86086e08a2fb334ccf`；其后交付只修改文档和视觉门禁编排，不改变通过验证的产品、测试、锁文件或迁移输入。最终 Git/PR 精确提交及远端检查快照另保存于工作区 `.local-dev/logs/r4-final-git-ci.json`，避免文档自引用提交哈希。

## 隔离与输入对应

后端使用独占 PostgreSQL `campusquest_test_r4_verify_20261013`、Redis 10；浏览器使用 `campusquest_test_r4_20261012`、Redis 9。两套世界与开发数据库分离。真实 MinIO、ClamAV、PostgreSQL、Redis 与生产组合均启用。没有重置开发数据或修改 main。

后端最终483个输入的 SHA256 为 `1efaa664c64175147f2da3111079e64710663bde538aa88c2f23de62870b53fb`；前后逐字节一致，`ef719f5..1e04425` 的 backend diff 为空。最终 Linux 前端394个源码、配置、契约、测试和资产输入的 SHA256 为 `b03dc2aff80687ca372a3005b806be36cfd06bc651ec9ac994cd07c0e53948de`；原仓库、完整浏览器副本与八门禁副本再次逐字节比较。只排除 Git 忽略的编译缓存 `*.tsbuildinfo` 和生成证书；Next自动生成、Git忽略的next-env.d.ts在恢复dev服务后仅两条类型路径变为 `.next/dev/types/`，严格按这两条路径还原build形式后摘要完全一致，没有忽略其他内容。最终交付检查同时读取浏览器 JSON 的零失败/零 flaky 统计。

完整日志在工作区 `.local-dev/logs/`；失败运行与准备运行保留，但不计完成。全量通过不声称零警告：后端有既有依赖弃用、约束测试和 submission worker 连接清理警告。前端审计沿用已审核 dev-toolchain 豁免，不声称没有任何 advisory。

## 最终本地门禁

| 检查 | 实际结果 | 完整日志 |
| --- | --- | --- |
| 后端 format/lint/mypy/矩阵/审计 | 全 exit 0；mypy213源文件、16份矩阵、无已知 Python 依赖漏洞 | `r4-backend-final3-20261013-report.md` 与同前缀日志 |
| 后端 unit + worker | 1402 passed、0 skip、2 warnings，exit 0 | `r4-backend-final3-20261013-unit-worker.log` |
| 后端真实PG integration | 1366 passed、7 deselected、29 warnings，exit 0；另7项全部显式运行通过 | `r4-backend-final3-20261013-integration.log`、`unmarked-checks.log` |
| 后端 E2E | 43 passed、0 skip，exit 0；上述不同套件合计2818通过 | `r4-backend-final3-20261013-e2e.log` |
| 全链迁移/结构等价 | 0001→0036→base→0036通过；254个迁移相关输入完全一致，最终alembic check退出0 | `r4-backend-verify-20261013-*migration*`、`r4-backend-final3-20261013-migration-evidence-identity.json` |
| 后端 fresh coverage | 全门槛保持；innovation97.9≥97.8，points95.3≥92.6 | `r4-backend-final3-20261013-coverage-ratchet.log` |
| Windows前端八门禁 | 类型、lint、CSS、unit、coverage、build、ratchet、audit全0；704单测、0skip；覆盖95.64/91.71/90.22 | `r4-final-windows-frontend-v5.log` 和 `r4-review-final-v5-windows-*.log` |
| Linux前端八门禁/契约新鲜度 | 全0；704单测、0skip；覆盖95.65/91.71/90.22；生成schema一致 | `r4-final-linux-frontend-v5.log` 和 `r4-final-linux-*.log` |
| 完整浏览器 | 152 passed、34具名豁免、0 failed/flaky；含13项人才操作、原成果/招募/任务、Firefox/WebKit烟测 | `r4-final-browser-stack-v3.log`、`r4-final-full-browser-report.json` |
| 严格no-skip | 所有spec均在报告；34项逐项符合既有或本轮明确门禁豁免，exit0 | 同上完整日志；新增人才像素项在独立视觉门禁真实运行 |
| 五组视觉，无重生成 | 原16 + R2 1 + R1 1 + R3 2 + R4 3 = 23 passed，均exit0 | `r4-final-browser-stack-v3.log` 与五份独立JSON报告 |
| CI/Make视觉编排 | YAML解析、五个独立命令、无update-snapshots；人才3场景收集成功。补人才CI入口及Make中R3/R4入口 | `r4-final-delivery-check.log` |
| 开发数据/服务 | 增量0034→0035→0036；原51张表行数完全相同。Web/API/Worker/Beat恢复，人才路由HTTP200、Worker pong | `r4-dev-before.json`、`r4-dev-after.json`、`r4-dev-migration.log`、`r4-demo-restored.log` |

前端地板95.21/91.22/90.19保持，既有epsilon不变。像素容差0.01保持，未用重拍或降低门槛强行通过。新增11张人才基线单独提交，旧R1两张仅反映已批准新增人才入口，其他旧图直接比较。

## 从用户视角验证

1. 无资格、无项目、无经历/作品/联系方式的正常学生可完成最小建档；默认私有，预览确认后才出现在校内人才库。自述明确区别于已核实成果。
2. 草稿保存不改变公开快照；确认更新才替换。暂不接洽、0小时投入与展示开关互相独立；标签全匹配、字面搜索、分页及返回筛选保持。
3. 文件检查通过仍私有，须明确选入公开版。真实上传、扫描失败重试、过期凭据检查已有对象、五文件整批替换与十库存限制已跑；联系值仅主动点击后读取。
4. 关闭档案/撤回授权后，观察者旧详情、联系和文件请求都拒绝；并发读入字节后仍重新验权，无法借旧链接绕过。本人继续维护，已下载副本无法回收。
5. 迟到响应不覆盖后来输入，账号切换清理私有恢复。跨窗口变更后返回以当前服务器公开/治理状态为准；同时编辑有比较入口，保留本地输入须明确选择。终止上传恢复可重选，未知结果保留原File与请求。
6. 手机长内容、加载/空/错误/权限状态、键盘纠错和预览焦点均验证；axe无新增豁免。实际屏幕阅读器未运行，不能用自动检查代替。

## 失败证据与作者修复

第一次后端完整集成有25项JWT过期401：四个旧fixture在收集时固定时间，长套件执行已超过15分钟。合成30分钟前anchor先RED4，再GREEN8；只修执行时种子和关联业务时间，不变更生产TTL、认证或Clock。第一次覆盖两域未达地板不计完成；补真实腐坏/存储故障/越权边界后，从全新coverage文件完整重跑得到上述结果。

独立review在 `ef719f5` 为REQUEST_CHANGES，两项Important：恢复旧record误述公开状态、终止grant仍锁定选文件。作者真实浏览器3RED→3GREEN，增强私有并发比较也先RED；最后13项人才浏览器及9项恢复纯测试通过。唯一作者修复提交 `1e04425`，最后完整套件全绿；没有第二轮独立APPROVE。详见[评审记录](innovation-talent-review.md)。

文件检查曾被开发代理30秒截断。早期 idle keep-alive竞态推断未充分解决；实际安装Next16.4.0代理源码默认30000ms，真实Next代理31秒响应测试RED500/socketreset→GREEN200。仅配置 `CQ_DEV_API_PROXY` 时设180秒代理预算；浏览器150秒、后端140秒边界不变。测试世界另外显式Uvicorn idle keep-alive60秒，不能据此断言唯一根因是闲置连接。生产入口的超时和正式发布仍属后续专门验收。

此前草稿脏判断、取消移除丢File、冲突比较不足、旧发布回执误述状态、手机长词溢出、私有治理状态缺失及隔离测试cleanup外键错误均有RED→GREEN记录，最终门禁包含相应回归。

## 保存与交付边界

沿用 `codex/innovation-platform`，推授权fork并保持[PR43](https://github.com/Penghuige/campus-quest/pull/43) Draft。上游当前冲突，完整GitHub CI尚未对本次头完成；GitGuardian不能代替完整CI。精确最终head与检查时间在本地最终快照，PR说明同步该次状态；不自动合并。

R4到此供体验。人才主动公开不套用成果审核，也不要求负责人资格。R2成果全部复审且待审保留旧通过版、R3独立免审招募仍受资格/项目权限控制；学校认证、导师库、运营治理与奖励仍按后续阶段推进。
