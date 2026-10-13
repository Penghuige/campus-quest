# R4.1 材料事实与人才UI验证回执

2026-10-13。本轮依据用户“展示简短方案后实施”的明确授权，继续R4.1，不重做R4、不进入R5。材料标志是当前公开附件可读状态事实，不是真实性认证；新生无材料仍可公开。

产品实现 `2c42e6e`，批准的人才PNG独立提交 `c12be11`，预览评审修复 `4a164b9`，矩阵/no-skip工件 `685e4de`。本回执随后只补文档；最终本地/fork/PR head与当前CI快照保存在工作区 `.local-dev/logs/r4-1-final-git-ci.json`，避免提交哈希自引用。

## 测试环境与输入对应

后端使用 PostgreSQL `campusquest_test_r4_verify_20261013` / Redis10；浏览器使用 `campusquest_test_r4_20261012` / Redis9，真实MinIO/ClamAV/Redis与生产组合启用。测试使用独立世界，不重置开发库；本轮没有数据库结构变更或开发库迁移。仅在必要时暂停已核验归属的Web3000，finally恢复，保留API/Worker/Beat。

后端483个输入在开始复制、完成验证和补7项测试后均与仓库逐字节一致，见 `r4-1-backend-final-source-manifest.json`、`r4-1-backend-final-source-still-current.log`、`r4-1-backend-close.log`。前端401个受版本控制输入在仓库、最终浏览器世界、最终Linux八门禁副本一致，SHA256 `c40f21c3f07807dd17d1644d16cede6b5929372165f250101f5e7a6b229416ed`，见 `r4-1-final-frontend-source-proof.json`。未把Next生成证书/编译缓存当产品输入。

## 已取得的新鲜结果

| 检查 | 本轮结果 | 完整日志（工作区 `.local-dev/logs/`） |
| --- | --- | --- |
| 后端format/lint/mypy | 全退出0；Linux mypy 213源文件 | `r4-1-backend-final-format.log`、`lint.log`、`mypy.log`（同前缀） |
| 16份规约矩阵 | 退出0；新增A13–A17使用规范四列与真实测试路径 | `r4-1-backend-close.log` |
| 后端unit/worker | 1402 passed，0 skip | `r4-1-backend-final-unit-worker.log` |
| 真实PG integration | 1371 passed，7 deselected；材料筛选/撤回/分页/N+1与原授权/并发均覆盖 | `r4-1-backend-final-integration.log` |
| 未标记7项检查 | 7 passed，1371 deselected；与上行完整并集 | `r4-1-backend-close.log` |
| 后端E2E | 43 passed，0 skip；不同套件合计2823通过 | `r4-1-backend-final-e2e.log` |
| Alembic结构检查 | check退出0，无需新迁移 | `r4-1-backend-final-migrate-check.log` |
| 新鲜coverage地板 | 保持；innovation97.9≥97.8，points95.3≥92.6 | `r4-1-backend-final-coverage-ratchet.log` |
| 后端依赖审计 | 同门禁重试退出0，No known vulnerabilities found | `r4-1-backend-audit-retry.log` |
| Windows前端八门禁 | 全退出0，709单测，0 skip；ratchet90.35函数≥90.19 | `r4-r4-1-reviewed-windows-*.log` |
| Linux前端八门禁/契约新鲜度 | 全退出0，709单测，0 skip；OpenAPI生成schema逐字节一致 | `r4-1-linux-frontend-validated-master.log`、`r4-1-validated-linux-*.log` |
| 具名视觉豁免/矩阵工件收尾 | 新增材料视觉项已实际独立运行；严格checker孤立探针先拒绝未登记项，再接受具名条目；双平台lint全0 | `r4-1-metadata-windows-lint.log`、`r4-1-metadata-linux-lint.log`；孤立探针目录 `.local-dev/r4-1-noskip-probe/` |
| 定点人才浏览器 | 14 passed；私有上传/明确公开/撤回/筛选/URL、恢复/冲突/跨账号/五文件替换 | `r4-1-browser-focused.log` |
| 五组像素，无update | 16基础 +1成果 +1导航 +2招募 +4人才，五组均退出0 | `r4-1-browser-all.log`、`r4-1-final-*-report.json` |

完整188项浏览器电池：**153 passed、35具名门禁豁免、0 unexpected、0 flaky**；严格no-skip退出0。五组视觉**24 passed、0 skipped/failed/flaky**。完整输出 `r4-1-browser-all.log`，最终统计 `r4-1-final-full-browser-report.json` 与5份独立视觉JSON；所有spec都在报告，新材料视觉项在独立门禁真实运行，没有隐蔽跳过。定点14项不替代这些完整结果。

## 失败与修复记录

1. 真实PG材料用例先RED（缺summary字段/参数422），后GREEN；URL true/false先RED再GREEN。初始“READY改回REJECTED”探针被既有数据库不可变保护拒绝，改为合法撤回/移除边界，保留保护，没有把环境或非法前置失败当业务RED。
2. Windows mypy受既有Linux resource.setrlimit接口影响报4错，Linux正式mypy通过；没有改动无关生产逻辑。
3. 首次后端综合脚本在临时源码目录无矩阵文档，matrix退出2；从实际仓库补跑16份矩阵成功。首次pip-audit无法连PyPI漏洞源，同门禁重试成功。首轮综合退出1如实保留；不能把原退出码改写为0，逐项成功与补门禁回执组成最终结果。
4. 独立评审在准确head `2c42e6e` 为REQUEST_CHANGES，一项P2混合失效附件、一个P3停用标签兜底；同次作者RED→GREEN修复及完整709套件通过，详见[评审回执](innovation-talent-r4-1-review.md)。没有第二轮reviewer APPROVE。
5. 新渲染回归首次引入整个确认交互模块，函数覆盖89.98低于既有地板。拆出真正独立的纯预览内容，继续测真实组件与完整字段/转义/0小时，不降地板或加排除；最终双平台90.35通过。
6. 旧人才像素4场景失败（新增缺图及授权布局尺寸变化），获准更新4场景捕获成功；随后所有新旧视觉无update比较通过。13图分别8批准变化/3字节一致/2新增，详见[逐图走查](innovation-talent-r4-1-visual.md)；其他页面没有重拍、容差保持0.01。

既有后端依赖弃用、submission连接清理等警告如实保留。前端沿用已审核dev-toolchain advisory豁免，不声称零advisory。axe与键盘检查通过不替代实际屏幕阅读器，真实读屏尚未执行。远端CI、学校SSO与合并批准不在本地通过结论内。

最终演示API仅重启已验归属8200进程，health200且OpenAPI含当前材料字段；Web借用结束finally恢复、Worker pong均以最终服务日志核验。本轮按后端与前端各自完整输出/退出码、JSON和源文件摘要交付，没有将截断聊天输出当唯一证据。
