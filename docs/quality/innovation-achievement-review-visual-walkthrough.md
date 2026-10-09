# 成果核实界面与基线走查

2026-10-09，本轮沿用 Ubuntu 24.04、Node 22.23.3、Playwright 1.63.0 / Chromium 153.0.8010.12、1440×900，固定 Noto Sans CJK SC 与 Liberation Mono。新增测试使用独占浏览器数据库，真实资格、运营授权和草稿接口；测试后恢复开发预览，不清理开发数据。

## 新的三张基线

`innovation-review-visual.spec.ts` 首轮缺少基线而失败，分别生成实际 PNG；逐张用图片查看工具走查后，第二轮比较通过（1 passed，35.5s），三个页面 axe 检查均无违规。

| PNG | 检查结果 |
| --- | --- |
| achievement-owner-proof-workspace-linux | 完整保留成果四字段、保存与私有预览、核实状态、文件要求、选择计数和提交入口；没有材料时提交禁用；资料和上传区别于人工核实 |
| achievement-operator-empty-queue-linux | 待办为空时给出解释；明确领取后才查看资料与证明、本人项目回避；重新读取及分页可见；空队列不展示负责人身份资料 |
| achievement-campus-empty-list-linux | 独立校内浏览页面、标题与返回工作台、空状态和分页完整；适用于所有正常校内角色，无学生专属侧栏依赖 |

功能浏览器测试另外覆盖 390px 宽度、键盘确认/Escape 焦点恢复、已领取的私密快照和公开成果详情的 axe；完整真实提交、退回、批准、免复审更新、响应丢失重试与同一浏览器跨标签换账号，不由截图测试替代。

## 两张有明确原因的既有基线

`student-achievement-draft-form-linux.png`：本计划改动说明文字。旧文本说“此处不收集私密证明材料”，新文本明确“保存个人草稿；首次核实通过或显式发布更新后，校内用户才能看到对应公开版本”。只有已保存且已开通资格的成果会显示证明区；新建空白表单不会冒出未保存成果的上传操作。对旧图和新图分别走查，其余四字段、布局、按钮保持一致。

`student-tasks-linux.png`：先复现旧基线差异，再追溯上游 `3a51680545b230745838df67c540d47497cc7b34`。该提交明确把领取和提交测试池分离，测试世界由 A/B/C/R 增为 A/B/C/D/R，但漏同步旧图。旧图显示 4/4、实际干净世界显示 5/5，差异来自第五张任务卡及可领取数，而非字体。当前视觉测试新增强断言：`.task-card` 必须为 5，页脚必须显示“已显示 5 / 5 个任务”，再采集有意同步的基线。没有修改任务业务代码、删断言、增加遮罩或放宽 0.01 像素比例。

更新仅限定这两张已走查的图，其他既有 PNG 保持不变。两张最终PNG与上传恢复修复之后，既有16场景新鲜比较通过（16 passed，1.3m，`review-existing-visual-final.log`）；新增三张PNG另开独立干净测试世界比较通过（1 passed，39.6s，`review-new-visual-final.log`），没有更新开关。原始失败及采集日志均保留在工作区 `.local-dev/logs/review-*-visual-*.log`、`review-target-baselines-*.log`。

先前 `innovation-linux-visual-walkthrough.md` 所述任务图缺口是历史状态，由本次明确断言与基线同步关闭；不把这次像素结果扩大为 GitHub CI 或学校统一认证完成。
