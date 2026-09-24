# E2E Selector Contract — Plan 11 visual refresh

> Status: **normative for the visual refresh workstream** (plan Task 1b).
>
> Inventory of every Plan 10 Playwright locator/assertion on screens the
> refresh touches, classified per the plan's rule:
>
> - **A — behavior/accessibility contract**: preserved AS-IS by the
>   visual work. These pin roles, accessible names, labels, dialog
>   semantics, and asserted business copy. Changing one is a product
>   change, not a restyle.
> - **B — pure structural locator**: may move with the new DOM only when
>   replaced by an **equally strong** locator/assertion in the same
>   spec. Never delete or weaken the underlying assertion.
>
> Reviewers diff visual PRs against this table. When a B locator moves,
> the PR must show the equal-strength replacement next to the DOM
> change. The zero-skip Playwright run gates every surface batch
> (plan Task 1b cadence).

Sources: `frontend/e2e/*.spec.ts` (auth, community, notifications,
rewards-ranking, staff-auth, staff-totp-race, submission, task-claim,
teacher, admin, visual-capture).

## A. Behavior / accessibility contracts

### Form labels (`getByLabel`)

| Label | Surface |
|---|---|
| 学号 / 密码 | student login (every spec's entry) |
| 邮箱 / 密码 / 动态验证码 | staff login |
| 设置密码 / 确认密码 / 设置密码并继续 | register, staff invite activation |
| 昵称 / 手机号 | register, profile binding |
| 任务评论 / 发布评论 / 发布回复 / 回复 | task-detail comment thread |
| 通知收件箱 / 标为已读 | notifications |
| 积分余额 (`getByLabel` + `[aria-label='积分余额']`) | student shell points readout |
| 我的附近 | rankings around-me |
| 操作原因 / 基础奖励积分 / 任务评分 | teacher task form, admin/moderation dialogs |
| 选择星级 (radiogroup) / 发布身份 (radiogroup) | review dialog, comment compose |

### Roles + accessible names

Buttons: 登录 · 注册 · 领取任务 · 开始上传 · 上传重试相关 · 兑换 · 确认兑换 ·
发布评论 · 发布回复 · 回复 · 赞 · 表情 🔥 · 举报 · 提交举报 · 标为已读 ·
完成 · 取消 · 创建草稿 · 保存修改 · 预览导入 · 重新上传文件 · 通过并发放奖励 ·
通过并扣减积分 · 退回修改 · 确认停用 · 确认绑定 · 确认揭示身份 · 获取验证码 ·
设置密码并继续

Links: 未读 · 热门 · 最新 · 全部 · 总榜 · 今日榜 · 本月榜 · 去登录 ·
前往员工登录 · 返回教师工作台 · 返回任务管理

Headings (`getByRole("heading")`): 登录 CampusQuest · 通知 · 确认兑换 ·
兑换申请已提交 · 审核提交 · 举报已提交

List: 恢复代码 (staff TOTP recovery codes)

### Dialog semantics (`aria-labelledby`)

Native `dialog` elements whose title ids the specs address directly:
`create-task-title` · `edit-task-title` · `import` preview surfaces ·
`lifecycle-confirm-title` · `redemption-approve-title` ·
`redemption-reject-title` · `redemption-fulfill-title` ·
`reveal-identity-title` · `setting-confirm-title` · `account-status-title`.

The refresh may restyle dialogs freely but must keep: the `dialog`
role (native element or equivalent), the labelledby wiring, and the
focus-trap/Escape behavior the app already implements.

### Asserted business copy (`getByText`, selection)

Error/validation copy: 退回说明不能为空 · 判无效原因不能为空 · 追溯原因必填
（将记入审计日志） · 操作原因必填（将记入审计日志） · 拒绝原因必填（将通过
通知送达申请者） · 评论内容不能为空 · 请选择举报类别 · 长度不合法 · 全角数字 ·
基础奖励积分必须大于 0 · 提交未通过校验，请修正 · 文件内重复（的 platform +
keyword 组合） · 第 2 行

Status/confirmation copy: 已提交，等待老师审核 · 重试完成提交 · 任务已完成，
积分已发放。 · 注册成功，请使用学号登录。 · 恢复代码只显示这一次 · 已复制 ·
动态口令绑定完成，员工账号已激活。 · 已成功导入 1 个任务单元 · 已发布 · 草稿 ·
已批准 · 待发放 · 已发放 · 已停用 · 启用 · 正常 · 累计获得 · 可花费/当前可花费/
消耗积分 · 匿名用户 · 暂无通知 · 暂无附近排名 · 判无效将取消该学生当前锁定的
奖励档位与积分

Access-denied copy (RBAC proofs): 教师工作台仅对教师与管理员开放 ·
管理后台仅对管理员开放，当前账号是教师账号 · 任务不存在，或您不是该任务的
所有者 / 协作者，无法访问。

These strings are asserted business semantics (they ARE the privacy /
RBAC / state-machine proofs). Copy may only change with an explicit,
owner-approved product decision — never "to fit the new DOM".

### data-testid

`totp-secret` · `totp-uri` (staff TOTP setup).

## B. Structural locators (equal-strength replacement allowed)

| Locator | Surface | Notes |
|---|---|---|
| `.page-head` | page header anchor (specs + visual-capture) | capture spec already falls back to `main`; if renamed, update all spec uses to the equivalent header locator |
| `.task-card` | tasks list | review-comment seed hook |
| `.claim-panel` · `.upload-panel` | task detail claim/upload | submission flow anchors |
| `.deadline-line` | task/claim deadline row | |
| `.validation-report` · `.report-errors` | submission validation report | |
| `.comment` · `.comment-list` · `.comment-author` · `.comment-depth-3` · `.reaction-count` | community thread | depth-3 asserts nesting |
| `.board-rows` · `.board-row` · `.board-rank` · `.board-me-tag` | rankings | me-tag asserts around-me anchoring |
| `.reward-card` | rewards list | |
| `.notif-item` · `.notif-item[data-read='false']` · `.notif-title` | notifications | data-read asserts unread state |
| `.review-item` · `.review-pair` · `.review-tier` | teacher review queue | pair/tier assert VALIDATED tier pairing |
| `.import-preview` · `.moderation-key` · `.mono` | teacher import / moderation | |
| `dialog.dialog` | generic dialog scoping | equal-strength replacement: `getByRole("dialog")` |
| `#submission-file` · `#assignment-import-file` | file inputs | keep as file-upload ids or replace with `getByLabel` of equal strength |
| `#task-schema` · `#task-schema-version` · `input[name='assignment_id']` | task form fields | |
| `[data-assignments-list]` · `[data-user-id]` | teacher assignments list, admin user rows | |

## Rules for the visual workstream

1. Class-A entries are frozen. A visual PR that changes one is out of
   scope until the owner rules it a product change.
2. A Class-B move ships in the same commit as the equal-strength spec
   update — never a "tests will be fixed later" state, and never a
   weakened assertion (e.g. replacing `.review-tier` with a loose
   text match that would also pass on the wrong tier).
3. The upload/download, review-decision, redemption, reveal, and
   lifecycle dialogs keep their confirmation-copy assertions even when
   their DOM is rebuilt.
4. New UI primitives that replace a B-class region must expose an
   equally specific hook (test id, class, or aria contract) at the
   same granularity.
