# R3 招募 Worker 与 Beat

生产入口保持 `app.workers.celery_app:celery_app` 的完整任务注册与原有扫描。新增 `workers.schedule_recruitments` 每 60 秒扫描一次，默认批次 100；配置为 `RECRUITMENT_SCAN_INTERVAL_SECONDS` / `RECRUITMENT_SCAN_BATCH_LIMIT`，必须为正数。

本地体验入口 `app.workers.innovation_dev:celery_app` 使用 `innovation-dev` 专用队列，Beat 只有招募维护与 `IE_RECRUITMENT_CONFIRMATION_DUE` 投递扫描。它不启动 Claim 到期或文件清理扫描，过滤默认参数为 None 的生产 dispatcher 仍处理全部事件。

本机 Windows 启动器在工作区 `.local-dev/Start-Dev.ps1` 增加 Worker/Beat 组件，调用 WSL `CampusQuest-Dev` 中自有 `campusquest-innovation-dev-runtime` 容器。容器只读挂载当前仓库，复用已安装 Linux Python 环境；日志和 Beat 计划写 `.local-dev/logs`，不包含联系值。启动 shell 隐藏，API/Web 沿用 8200/3000。

Linux 辅助脚本 `backend/scripts/innovation_dev_runtime.py` 保存 PID、进程开始时间和组件身份。停止前同时验证 `/proc` 中开始时间、Celery profile、worker/beat 参数及独有 hostname/pidfile；只向匹配进程发信号。Windows 停止器随后按原归属检查停止宿主子进程，保留数据库与存储卷；不 purge 队列或关闭别人的 worker。

本机启动前应先正常执行 additive `alembic upgrade head`。开发库只使用真实 SystemClock，预置演示账号保留，启动器不创建/发布岗位或改时钟。

验证用 R3 数据库与 Redis 10，浏览器用独立数据库与 Redis 11。真实 Worker/Beat 启动验证使用 `r3-test` 身份，与本地体验 `local` 身份分开；测试结束检查并停止它们。30 天边界、停机补偿和竞争在真实 PostgreSQL 配合 FrozenClock 验证，不为演示伪造通知。

体验时本人管理页展示真实截止和下次提醒；无日期岗位通过“确认继续招募”开启新周期。提醒需要 Worker/Beat 正常运行、通知已真实完成 IN_APP 投递，才进入 Inbox；岗位关闭、到期、下架、负责人失效或旧周期均在发送时抑制，保留原通知历史。
