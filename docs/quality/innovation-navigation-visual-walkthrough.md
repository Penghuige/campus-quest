# R1 固定字体截图走查

2026-10-10，Linux Ubuntu，Noto Sans CJK SC / Liberation Mono，起点 `971b16c`。完整日志：工作区 `.local-dev/logs/r1-existing-visual-before.log`、`r1-existing-visual-update.log`、`r1-visual-final.log`。未修改比较阈值、既有遮罩或字体契约。

## 有意更新的 17 张历史基线

对每张截图生成旧／新并排图，检查导航、标题、内容完整性及页面底部。并排图保存在仓库外 `.local-dev/r1-visual-review/`。下列变化符合 R1；不是给未知失败重新拍照。

| 截图（均为 `-linux.png`） | 新旧变化与走查结论 |
| --- | --- |
| achievement-campus-empty-list | 校内成果从独立顶栏进入学生公共导航壳；成果空状态及分页保留 |
| achievement-operator-empty-queue | 新增创新创业主入口，运营标题改“双创运营工作台”，仅运营当前项高亮；队列保留 |
| achievement-owner-proof-workspace | 新入口、独立运营菜单和退出；私有表单、保存提示、证明与提交区保留 |
| admin-innovation-operations | 新增创新创业及退出；授权列表、分页保留 |
| admin-owner-qualifications | 新增创新创业及退出；资格待办空状态保留 |
| admin-users | 新增导航和退出，账号区域高度增加 22px，整页 1356→1378px；用户表及底部总数完整 |
| student-achievement-draft-form | 新入口和退出；表单、保存/预览区保留 |
| student-claim | 新入口和退出；任务信息、材料、提交区保留 |
| student-dashboard | 新入口和退出；余额、排名、任务卡、空状态及原模拟数据保留 |
| student-notifications | 新入口和退出；过滤器和空状态保留 |
| student-owner-profile-form | 新入口和退出；四项资料、资格状态、申请区完整 |
| student-project-draft-form | 新入口和退出；项目字段、保存区完整 |
| student-rankings | 新入口和退出；当前排名和列表保留 |
| student-rewards | 新入口和退出；余额、兑换卡保留 |
| student-task-detail | 新入口和退出；任务详情、评论和分页完整 |
| student-tasks | 新入口和退出；任务卡和底部计数保留 |
| teacher-reviews | 新增创新创业及退出；原审核队列与详情空状态保留 |

## 新增 3 张基线

- `innovation-campus-landing-linux.png`：模拟资料/未接 SSO 提示、真实成果空状态、三库范围、本人和运营分流清楚。
- `innovation-mobile-full-menu-linux.png`：390px，全导航包含奖励、通知和授权运营；五个底栏仍可辨认，无横向溢出。菜单角色与 Escape 关闭另由功能测试覆盖。
- `innovation-logout-confirmation-linux.png`：独立确认页卸载私密工作区，按钮和取消链接可达，无旧草稿内容。

首次新增基线后，再运行不带更新参数的比较：原有 16 场景 PASS，成果核实 1 场景/3 PNG PASS，R1 1 场景/3 PNG PASS，合计 22 PNG。后两场景均执行 axe 并为 0 违规；既有 axe 历史阈值不作清零宣称。登录、组件画廊及 11 张未被当前测试引用的历史 `-chromium-linux.png` 原样保留。

基线独立提交；测试、选择器契约、no-skip 豁免和 CI 独立阶段在同一 Draft PR 交付。
