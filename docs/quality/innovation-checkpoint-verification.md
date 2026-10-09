# 双创准备区验证记录

2026-10-09。本记录对应私有负责人资料、项目/成果草稿和双创运营授权的开发检查点，不代表三库 demo 或合并验收已完成。

## 新鲜采集与覆盖率登记

- 后端源码、测试与采集配置：`18650e32dc09b6ceba6a3a26a1df4590925ad9e0` 的归档；后续提交只增加 PNG、验证文档和 innovation 的覆盖率条目，不改生产代码或测试逻辑。
- Linux Python 3.12.14 / Coverage 7.16.2，声明 `greenlet` 和 `thread`；使用新建独立 PostgreSQL 库、真实 Redis/MinIO，开启 `CQ_S3_SMOKE=1` 和 `CQ_COMPOSITION_SMOKE=1`。未使用开发库或共享 `campusquest_test`。
- 使用全新的 `COVERAGE_FILE`：先运行 `pytest tests/unit tests/workers --cov=app --cov-branch --cov-report= -q`，再运行 `pytest tests/integration -m integration --cov=app --cov-branch --cov-report= --cov-append -q`。不合并任何旧配置采集的数据。
- 单元/worker：**1266 passed**，318.44 秒，退出码0。集成：**1117 passed, 7 deselected**，816.88 秒，退出码0；该命令的 integration 标记筛选仍保留，不把7项未选中用例宣称为已执行。集成有29项既有警告，包括连接生命周期警告，未据此宣称零警告。
- Ruff 格式/检查、mypy（174个源文件）、11份矩阵检查通过。
- 原17个覆盖率地板均达到；首次比较唯一失败为新模块未登记。运行项目 `coverage_ratchet.py --update` 在隔离副本生成18模块候选，仅采纳 `app/modules/innovation: 97.8`。其余17项地板和 epsilon 保持原值；积分实测95.3，原地板92.6。
- 将登记后的仓库地板复制回隔离副本，使用相同完整新数据重新比较：`ratchet holds`，退出码0；日志为 `.local-dev/logs/innovation-greenlet-final-ratchet.log`。最初综合脚本因未登记返回1，不能将其原始退出码改写为0。
- 97.8是仓库现有棘轮算法的实测指标，不是对全部业务需求的覆盖声明。新地板仍待最终 HEAD 的 CI 校验，不能把本机 Linux 结果当成 GitHub CI。

此前未声明 greenlet 的报告出现跨文件行号错位（六行源码被记录为第103行），其覆盖率数字全部弃用。回归测试使用真实 SQLAlchemy async bridge，旧配置失败、正确配置通过。收紧重复发奖用例的双连接竞争调度只改变测试，不改变发奖规则。

本机完整日志位于工作区 `.local-dev/logs/linux-18650e3-greenlet-backend.log`，原始新数据与 JSON 同目录；这些本机工件不在 Git 内，其他检出需重新运行采集命令。

## 前端与视觉范围

- 前端静态检查、正式构建和棘轮在 `4cb65a625ca06da071d83b75c301fe88f37c5709` 上重新运行，均退出码0；Node 22.23.3。棘轮实测 lines95.38 / branches91.4 / functions90.18，既有地板与 epsilon 未改。
- 四张双创新 PNG 逐张人工查看，同一固定字体的 Linux 环境重复运行 **4 passed**；既有11项像素场景10项通过、任务列表1项失败。生产代码来源、临时独立库 DSN 适配和内容差根因见 [Linux像素走查](innovation-linux-visual-walkthrough.md)。既有 PNG 未重拍，阈值未放宽。
- Python 依赖审计未发现已知漏洞；前端审计门禁通过，但保留仓库现有5项开发依赖链豁免，不表示全部依赖无漏洞。

## 尚未关闭的交付条件

完整像素门禁、完整发布电池、最终 HEAD 的 CI 和 owner 产品验收仍待完成。负责人资格开通操作尚需业务决定；成果证据附件、提交核实与校内展示、招募、人才和导师库不属于当前已完成行为。
