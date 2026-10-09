# 负责人资格：Linux视觉与浏览器走查

2026-10-09，依据用户确认的方案A：保存四项资料后显式申请，由ADMIN人工开通。资格面板和ADMIN页面属于这次已授权的产品变更。它们不代表学校统一认证或成果核实完成。

资格功能源码与测试提交为 `1e440fa`；本提交仅新增/更新下述两张PNG及走查记录，不改变生产代码。浏览器在对应工作树执行，最终SHOTS准备条件与该源码提交一致。后端全量与随后权限补验的分批证据见资格规约矩阵；本记录不把工作树验证改称最终GitHub HEAD CI。

## 环境与数据边界

沿用 `innovation-linux-visual-walkthrough.md` 的Ubuntu 24.04、Node22.23.3、Playwright1.63/Chromium1243、固定版本Noto CJK与Liberation Mono。断言仍为fullPage、maxDiffPixelRatio=0.01；不新增mask或放宽容差。

归档当前资格源码树，在独立Linux容器运行；仅归档副本的Playwright配置与global-setup DSN临时指向 `campusquest_test_ie_qualification_browser`。应用生产源码不改写。该库从空库迁移到0029，0029→0028→0029及Alembic check均退出0。重拍与复验前全表清理只作用于该专属测试库；SQL先校验current_database，保留alembic_version。没有清理本机开发库或共享campusquest_test。

## 基线选择与逐张走查

| 画面 | 首次比较 | 走查与处理 |
|---|---|---|
| admin-owner-qualifications | 新页面，无已有PNG | 新增1440×900 Linux基线。空队列明确提示学生先申请；管理导航标明负责人资格。队列未载入四项PII。 |
| student-owner-profile-form | 原1440×900，现1440×1128 | 确认内容变更：原四项输入、字段限制、保存行为保持；增加独立资格面板、状态、申请与重读按钮；将旧“开通方式待确定/仅自己可见”文案改为已确认的申请快照流程。因真实产品变更仅更新这一张旧基线。 |
| admin-innovation-operations | 通过既有比较阈值 | 新导航可达，原运营管理页面保持原PNG字节，不重拍。通过不意味着每个渲染像素与旧图相同。 |
| admin-users | 通过既有比较阈值 | 原用户管理功能与mask契约保持；原PNG字节不变。 |

上述新旧画面与最终两张PNG均人工查看。新增/更新PNG使用独立提交，其他既有PNG不更新。首次4页比较为2通过/2失败：新资格页缺基线、负责人资料页尺寸因新增面板变化；不是运行异常。仅针对这两页生成后2项通过。清库后全16页重复比较：15通过/1失败；本轮4个涉及资格/管理导航的页面全部通过。

唯一失败仍为student-tasks，15142不同像素，和既有检查相同。先前已追溯上游3a51680新增独立任务D而旧PNG未同步。该页生产源码/CSS不是本轮改动，不重拍它以取得绿色。

## 实际交互与无障碍

- 资格新增3项浏览器用例通过：真实申请/ADMIN查看及确认/学生重读，跨标签换账号清除本人资料；新快照使旧管理员页面409；实际已开通但回包丢失时先重读；后续队列页刷新为空仍能返回上一页。
- 既有资料2项、运营2项、项目草稿3项、成果草稿3项通过，共13项双创功能。真实审批不是浏览器mock；分页回归仅mock列表读响应，独立证明导航状态。
- 390px手机测试包含键盘Enter、取消/Escape焦点恢复、开通后的重读焦点、无页面横向溢出；管理员队列没有四项PII，显式打开详情后才可见。
- 最终axe16页全部通过原双向棘轮，未增加或删除无障碍豁免。中间两次失败分别扫描了task卡片淡入中间态、claim加载态；失败记录保留。临时诊断将cq-rise固定到40ms：祖先opacity约0.633979时产生同类对比度告警，动画结束后相同颜色只剩既有heading-order条目。SHOTS现在等待首张任务卡和有限动画结束、claim精确区域可见；没有改产品颜色来掩盖采样问题。

手机/开通后桌面行为截图在本机日志目录旁保存，用于交互走查，不冒充固定字体像素基线。完整输出位于工作区 `.local-dev/logs/`：`qualification-browser-module.log`、`qualification-browser-drafts.log`、`qualification-axe-content-ready.log`、`qualification-visual-compare.log`、`qualification-visual-update.log`、`qualification-visual-final-matrix.log`。全站像素门禁仍失败；这份记录不构成合并或上线批准。
