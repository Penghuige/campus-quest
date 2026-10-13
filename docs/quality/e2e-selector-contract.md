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
| region `本人人才档案编辑器` · status `人才档案操作结果` · region `最新人才版本` | private talent profile | exact labeled fields; 保存人才草稿 / 预览开启校内展示 / 预览更新公开档案; original request retry preserves later edits; conflict resolution is explicit |
| group `作品与证明文件（选填）` · status `人才文件操作结果` · `#talent-upload` | private checked talent uploads | functional specs use label 选择作品或证明文件 and checkbox 公开 {name}; cancellation preserves original File and request; removal/revocation requires confirmation; 清除已移除的文件选择 only clears selections absent from the authoritative list and preserves text |
| region `校内人才库` · form `人才筛选` · nav `人才分页` · article `校内人才详情` | campus talent directory | URL carries keyword, all-match tags, cooperation state and offset; 返回人才列表 keeps filters; no private identity or contact values in ordinary browsing |
| region `人才授权联系` · status `人才联系读取结果` · region `公开作品文件` | deliberate sensitive talent reads | 查看授权联系方式 / 下载 {name} trigger current-version authorized requests; live status never reads contact values aloud; unavailable states contain neutral messages |
| region `负责人资料编辑器` · region `最新负责人资料` | private owner profile | fields use exact labels 姓名/学号/专业/年级; current account only; owner-profile API is not opened to ADMIN |
| status `负责人资料保存状态` | private owner profile save announcement | exact role/name scopes save assertions now that the page also contains qualification status; preserve the save success/error behavior |
| region `负责人资格` · status `负责人资格状态` · status `资格操作结果` | student owner qualification | qualification state and mutation result are independently named; no user ID, approver identity or another student's PII in the student DOM |
| region `负责人资格申请队列` · table `负责人资格申请列表` · nav `资格申请分页` | ADMIN owner qualification queue | paginated pending queue contains account references/state/time only; snapshot PII is not fetched or rendered before explicit 查看申请 |
| region `负责人资格申请详情` · status `申请开通状态` · status `管理员资格操作结果` | ADMIN application snapshot | four submitted snapshot fields are available only through an ADMIN-authorized, audited detail read; never infer this access from operations_enabled |
| dialog `开通负责人资格` · title id `owner-qualification-confirm-title` | ADMIN qualification confirmation | use getByRole("dialog", { name: "开通负责人资格" }); keep title wiring, focus trap, Escape and explicit 确认开通/取消 actions |
| region `成果草稿列表` · region `成果草稿编辑器` · region `最新已保存成果` · region `私有成果预览` | private project achievements | exact labels 成果名称/作品与阶段成果说明/作品链接/立项或获奖说明; no internal IDs in visible copy |

Private innovation draft behavior contracts (R1 navigation rename): link `我的项目`; regions `项目草稿列表` / `项目草稿编辑器` / `最新已保存版本`; buttons `新建项目草稿` / `保存草稿` / `返回草稿列表` / `编辑项目：{title}` / `读取最新版本（保留当前输入）` / `载入此版本（替换当前输入）`; saved status `草稿已保存，仅自己可见。`; stale-save copy `你的未保存内容已保留`.

Private owner profile contracts (R1 navigation rename): link `负责人资料与资格`; buttons `保存负责人资料` / `读取最新资料（保留当前输入）` / `载入此资料（替换当前输入）`; status name `负责人资料保存状态`, saved copy `资料已保存；保存资料不会自动开通负责人资格。`; conflicts preserve inputs until explicit reconciliation. The student number is allowed in this owner's private form by the 2026-10-09 human-supplied innovation requirements. Scheme A adds the narrow exception that ADMIN may explicitly read a submitted qualification snapshot through its separate audited detail endpoint; the original owner-profile endpoint remains owner-only.

Owner qualification contracts (2026-10-09 confirmed scheme A): student region `负责人资格`; named statuses `负责人资格状态` / `资格操作结果`; buttons `申请负责人资格` / `更新资格申请` / `重新读取资格状态`. Unsaved profile changes are not submitted with an application. PENDING uses the submitted saved-profile snapshot; a newer saved profile can explicitly update it. APPROVED cannot be overwritten by another application and does not approve achievements. Conflict or unknown write outcomes block another mutation until a successful reread; rereading a newer owner profile preserves inputs until explicit loading in `最新负责人资料`.

ADMIN qualification contracts: route `/admin/owner-qualifications`, navigation link `负责人资格`; regions `负责人资格申请队列` / `负责人资格申请详情`; table `负责人资格申请列表`; buttons `刷新申请队列` / `上一页申请` / `下一页申请` / `查看申请` / `开通负责人资格` / `重新读取申请详情`. Dialog `开通负责人资格` carries `确认开通` and `取消`. Cancelling or pressing Escape restores focus to `开通负责人资格`; after success or an unknown outcome disables that action, closing restores focus to `重新读取申请详情`. Busy submission guards closing through onOpenChange. Role/name locators must retain these behavior/accessibility assertions; this document does not claim they have passed a browser run.

Innovation operations contracts: regions `双创运营授权管理` / `当前账号运营授权`; labels `授权对象` / `操作原因`; buttons `授予运营身份` / `撤回运营身份` / `重新读取授权状态`; dialog `撤回双创运营身份` with `确认撤回` and `取消`. These are role/label selectors; no UUID text, CSS ancestry or fixed account nickname is required. Unknown write outcomes and 409 block further mutation until a successful reread.

## Rules for the visual workstream

Achievement-review additions (2026-10-09): A-contract regions `成果核实与证明`, `成果核实待办`, `成果核实快照`, article `校内成果详情`, and `校内已核实成果`; labels `上传证明材料`, `核实备注／退回原因`, checkbox `选择证明 {n}`. Preserve confirmation dialogs `提交首次核实`, `撤回首次核实`, `退回成果修改`, `通过首次核实`, `发布更新`, with exact `确认`/`取消`, Escape and opener focus restoration. Preserve actions `确认上次提交结果（沿用原请求）` and `确认上次决定结果（沿用原请求）`; the functional spec compares actual replay bodies after a real server commit with a lost response. Upload recovery preserves `重试原文件上传与检查` and `放弃本次上传重试`; explicit abandonment clears only the local file/key and never deletes server evidence. B-contract: reuse `.ie-draft-list`, `.ie-draft-row`, `.ie-draft-actions` for owner and queue structure; `.ie-review-prose` preserves literal multiline public content. These classes are presentation hooks, while role/label assertions remain authoritative. The campus shell accepts all active roles; R1 reuses the student shell for ACTIVE students and retains explicit staff workspace return links. Pixel assertions anchor the public region and wait for capability navigation.

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

R1 navigation/logout contracts (2026-10-10 owner-authorized product change): primary link `创新创业` (/innovation); nav `本人双创管理` with `我的项目` / `负责人资料与资格`; permission-scoped `双创运营工作台` (/innovation/reviews); mobile five destinations plus button/dialog/nav `全部导航`. Three-library region `双创三库` identifies unopened talent/mentor libraries. Following the owner's placement adjustment, students reach the single `退出登录` link in region `退出当前账号` at the bottom of /profile, after either growth or personal-information content; the student banner has no logout link. Staff account areas retain their exit links. All exits lead to the independent confirmation page; heading/region `退出当前账号`, actions `确认退出登录` / `重试退出登录`, alert `退出结果`. Real 503/transport failures remain on /logout; the logout endpoint's exact 401 AUTHENTICATION_REQUIRED verdict means no refresh credential remains. Student proof and operator decision dialogs fall back to the stable result section or original-request retry button when the opener disappeared; no focus restore across auth epochs.
R1 review fixes: 15-second bounded logout failure permits retry without racing a pending refresh; native full-document login respects CQ_BASE_PATH (independent of API mount). Successful original-command retries also focus the stable proof/result section after render, within the same auth epoch.

R1 owner-requested visual simplification: the profile's `退出当前账号` region keeps its accessible name but displays only the `退出登录` link, without a visible heading or explanatory paragraph. Student banner link `我的账户` retains its accessible name, title and /profile destination; its decorative glyph reuses the shared UserIcon line portrait rather than a nickname initial. Sidebar nickname display is unaffected; no selector or API contract changes.

R2 update review contracts (2026-10-10 owner-authorized product change): reuse region `成果核实与证明`, draft button `编辑成果：{title}` and public article `校内成果详情`. Owner actions `提交更新复审` / `撤回更新复审` and operator action `通过更新复审` distinguish the active UPDATE case from first approval. Pending, returned and withdrawn copies explicitly preserve the old approved version; taken-down copies preserve takedown. Public `本版核实` derives from the current revision's approved case; a legacy unreviewed update says `历史更新内容，未逐项复审。`. Original confirmation, retry payload, auth-epoch and focus assertions remain. Two new region screenshots cover update-pending and update-approved states; only runtime `<time>` values are masked on the approved view, with functional tests checking both timestamps.

R3 recruitment contracts (2026-10-11 approved design, 2026-10-12 owner-authorized UX revision): management regions `招募岗位列表` / `招募岗位编辑器` / `最新招募版本`, status `招募操作结果`; actions `新建招募岗位` / `保存招募草稿` / `预览发布招募` / `发布招募更新` / `关闭招募` / `重新开放招募` / `确认继续招募` / `撤销联系授权：{渠道}`. Preserve `重试原请求` with the original payload/request key and explicit `读取最新版本（保留当前输入）` / `载入最新版本`. Confirmation dialogs retain `取消`, Escape/opener focus and success focus on the stable result status; primary buttons now name the action: `确认发布` / `确认关闭` / `确认重新开放` / `确认继续招募` / `确认撤销授权` / `撤销并关闭招募`. The recruitment editor's `项目阶段` is one select; `具体阶段` appears for other stages, with no separate `创意起步` boolean control. Private project drafts retain their existing text `项目阶段` field. Public regions/articles `校内项目列表` / `校内项目详情` / `校内招募岗位` / `校内招募详情` / `项目已核实成果` / `项目正在招募` / `授权联系方式`; `查看联系方式` is the only deliberate contact read trigger. B-contract uses `.ie-recruitment-fields` / `.ie-recruitment-confirm` / `.ie-recruitment-public` for layout only; role/name/label behavior remains authoritative. Filtering is the named GET form `校内浏览筛选`; `仅看正在招募` is present in the project library and absent from the already active-only recruitment list. Local URL parameters and specific pagination names remain.

Recruitment contact correction (2026-10-12): missing channels focuses the 授权微信 checkbox; malformed selected values focus the matching `contact-{channel}` input. Each invalid input has `aria-invalid=true` and its own associated `contact-{channel}-error`; unselected-channel checkbox errors refer to `recruit-contacts-error`.

Recruitment acceptance fixes (2026-10-12): same-tab route recovery returns directly to the unsaved editor without automatic saves; explicit local 返回招募列表 discards only after confirmation when dirty. New/edit headings 新建招募岗位 / 编辑招募岗位 are focusable on entry, and returning restores the create/edit action. A workspace 404 says 项目不存在或不可访问, preserving its neutral privacy meaning and 返回我的项目; temporary errors retain 重试. Idea-only 阶段目标 / 初步方案 are absent outside idea stage, except an invalid preserved field temporarily reappears for correction. Contact fields retain their individual error associations and add channel format hints/input semantics. Persistent status 联系读取结果 announces 联系方式已读取，请查看下方已授权渠道。 without containing contact values. Public beginner false says 不接受零基础. Only normal, open, undated roles show 下次提醒.
