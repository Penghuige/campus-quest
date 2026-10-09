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
| 项目名称 / 项目简介 / 项目方向 / 项目阶段 / 团队现状 | private innovation draft editor |

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

Dialog surfaces whose title ids the specs address directly (native
`<dialog>` before plan-14; the Radix-backed `components/ui/dialog`
primitive since — role=dialog + labelledby preserved byte-identical):
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
| `.task-card` | tasks list | review-comment seed hook; SHOTS `student-tasks` preparation waits for the first card to be visible, then awaits `finished` for finite animations before sampling |
| `.claim-panel` · `.upload-panel` | task detail claim/upload | submission flow anchors |
| `.deadline-line` | task/claim deadline row | SHOTS `student-claim` preparation first waits for the exact named region `分配给你的任务单元` to be visible, so capture/axe observes loaded claim content |
| `.validation-report` · `.report-errors` | submission validation report | |
| `.comment` · `.comment-list` · `.comment-author` · `.comment-depth-3` · `.reaction-count` | community thread | depth-3 asserts nesting |
| `.board-rows` · `.board-row` · `.board-rank` · `.board-me-tag` · `.board-honor` | rankings | me-tag asserts around-me anchoring; board-honor is the honor chip (renamed from `.honor-chip` in plan-13 with no spec uses) |
| `.reward-card` · `.balance-quiet` · `.reward-cta-note` | rewards list | balance-quiet is the wallet's 累计获得 value (re-anchored from a parent-hop locator in plan-13); reward-cta-note is the spend-state note under the CTA |
| `.notif-item` · `.notif-item[data-read='false']` · `.notif-title` | notifications | data-read asserts unread state |
| `.review-item` · `.review-pair` · `.review-tier` | teacher review queue | pair/tier assert VALIDATED tier pairing |
| `.import-preview` · `.moderation-key` · `.mono` | teacher import / moderation | |
| `dialog.dialog` | generic dialog scoping | equal-strength replacement: `getByRole("dialog")` |
| `#submission-file` · `#assignment-import-file` | file inputs | keep as file-upload ids or replace with `getByLabel` of equal strength |
| `#task-schema` · `#task-schema-version` · `input[name='assignment_id']` | task form fields | |
| `[data-assignments-list]` · `[data-user-id]` | teacher assignments list, admin user rows | |
| `.ie-draft-list` · `.ie-draft-row` · `.ie-draft-form` | private innovation drafts | list/form boundaries; functional tests prefer named regions and labels |
| region `负责人资料编辑器` · region `最新负责人资料` | private owner profile | fields use exact labels 姓名/学号/专业/年级; current account only; owner-profile API is not opened to ADMIN |
| status `负责人资料保存状态` | private owner profile save announcement | exact role/name scopes save assertions now that the page also contains qualification status; preserve the save success/error behavior |
| region `负责人资格` · status `负责人资格状态` · status `资格操作结果` | student owner qualification | qualification state and mutation result are independently named; no user ID, approver identity or another student's PII in the student DOM |
| region `负责人资格申请队列` · table `负责人资格申请列表` · nav `资格申请分页` | ADMIN owner qualification queue | paginated pending queue contains account references/state/time only; snapshot PII is not fetched or rendered before explicit 查看申请 |
| region `负责人资格申请详情` · status `申请开通状态` · status `管理员资格操作结果` | ADMIN application snapshot | four submitted snapshot fields are available only through an ADMIN-authorized, audited detail read; never infer this access from operations_enabled |
| dialog `开通负责人资格` · title id `owner-qualification-confirm-title` | ADMIN qualification confirmation | use getByRole("dialog", { name: "开通负责人资格" }); keep title wiring, focus trap, Escape and explicit 确认开通/取消 actions |
| region `成果草稿列表` · region `成果草稿编辑器` · region `最新已保存成果` · region `私有成果预览` | private project achievements | exact labels 成果名称/作品与阶段成果说明/作品链接/立项或获奖说明; no internal IDs in visible copy |

Private innovation draft behavior contracts: link `我的项目草稿`; regions `项目草稿列表` / `项目草稿编辑器` / `最新已保存版本`; buttons `新建项目草稿` / `保存草稿` / `返回草稿列表` / `编辑项目：{title}` / `读取最新版本（保留当前输入）` / `载入此版本（替换当前输入）`; saved status `草稿已保存，仅自己可见。`; stale-save copy `你的未保存内容已保留`.

Private owner profile contracts: link `负责人资料`; buttons `保存负责人资料` / `读取最新资料（保留当前输入）` / `载入此资料（替换当前输入）`; status name `负责人资料保存状态`, saved copy `资料已保存；保存资料不会自动开通负责人资格。`; conflicts preserve inputs until explicit reconciliation. The student number is allowed in this owner's private form by the 2026-10-09 human-supplied innovation requirements. Scheme A adds the narrow exception that ADMIN may explicitly read a submitted qualification snapshot through its separate audited detail endpoint; the original owner-profile endpoint remains owner-only.

Owner qualification contracts (2026-10-09 confirmed scheme A): student region `负责人资格`; named statuses `负责人资格状态` / `资格操作结果`; buttons `申请负责人资格` / `更新资格申请` / `重新读取资格状态`. Unsaved profile changes are not submitted with an application. PENDING uses the submitted saved-profile snapshot; a newer saved profile can explicitly update it. APPROVED cannot be overwritten by another application and does not approve achievements. Conflict or unknown write outcomes block another mutation until a successful reread; rereading a newer owner profile preserves inputs until explicit loading in `最新负责人资料`.

ADMIN qualification contracts: route `/admin/owner-qualifications`, navigation link `负责人资格`; regions `负责人资格申请队列` / `负责人资格申请详情`; table `负责人资格申请列表`; buttons `刷新申请队列` / `上一页申请` / `下一页申请` / `查看申请` / `开通负责人资格` / `重新读取申请详情`. Dialog `开通负责人资格` carries `确认开通` and `取消`. Cancelling or pressing Escape restores focus to `开通负责人资格`; after success or an unknown outcome disables that action, closing restores focus to `重新读取申请详情`. Busy submission guards closing through onOpenChange. Role/name locators must retain these behavior/accessibility assertions; this document does not claim they have passed a browser run.

Innovation operations contracts: regions `双创运营授权管理` / `当前账号运营授权` / `我的双创身份`; labels `授权对象` / `操作原因`; buttons `授予运营身份` / `撤回运营身份` / `重新读取授权状态` / `重新读取身份`; dialog `撤回双创运营身份` with `确认撤回` and `取消`. These are role/label selectors; no UUID text, CSS ancestry or fixed account nickname is required. Unknown write outcomes and 409 block further mutation until a successful reread.

## Rules for the visual workstream

Achievement-review additions (2026-10-09): A-contract regions `成果核实与证明`, `成果核实待办`, `成果核实快照`, article `校内成果详情`, and `校内已核实成果`; labels `上传证明材料`, `核实备注／退回原因`, checkbox `选择证明 {n}`. Preserve confirmation dialogs `提交首次核实`, `撤回首次核实`, `退回成果修改`, `通过首次核实`, `发布更新`, with exact `确认`/`取消`, Escape and opener focus restoration. Preserve actions `确认上次提交结果（沿用原请求）` and `确认上次决定结果（沿用原请求）`; the functional spec compares actual replay bodies after a real server commit with a lost response. Upload recovery preserves `重试原文件上传与检查` and `放弃本次上传重试`; explicit abandonment clears only the local file/key and never deletes server evidence. B-contract: reuse `.ie-draft-list`, `.ie-draft-row`, `.ie-draft-actions` for owner and queue structure; `.ie-review-prose` preserves literal multiline public content. These classes are presentation hooks, while role/label assertions remain authoritative. The separate campus shell intentionally accepts all active roles and its pixel test anchors the named public region rather than the student sidebar.

Private achievement contracts: project editor link `管理成果草稿`; buttons `新建成果草稿` / `保存成果草稿` / `返回成果列表` / `编辑成果：{title}` / `读取最新版本（保留当前输入）` / `载入此版本（替换当前输入）` / `重试这次新建（不会重复创建）` / `查看私有预览`; status `成果草稿已保存，仅自己可见。`. Unknown create outcomes freeze the original payload and retry key. Unknown edits and conflicts require explicit reconciliation. Preview renders literal text and never fetches work URLs.

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
