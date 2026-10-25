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
teacher, admin, visual-capture) — the original Task 1b inventory. Specs
added since are not re-listed here; each lands its own entries per the
AGENTS.md test-artifacts manifest, and the A/B tables are the
authoritative record.

## A. Behavior / accessibility contracts

### Form labels (`getByLabel`)

| Label | Surface |
|---|---|
| 账号 / 验证码 | the unified login's code form (the band rides the PLACEHOLDER: 请输入手机号或邮箱 — the 2026-10-20 refinement) |
| 账号 / 密码 | the unified login's password expansion (placeholder 请输入手机号、邮箱、学号 — three-state incl 学号) |
| 动态验证码 | the unified login's TOTP leg (layer 2a, testid `unified-login-totp-leg`) |
| 设置密码 / 确认密码 / 设置密码并继续 | register, staff invite activation |
| 昵称 / 手机号 | register, profile binding |
| 受邀人邮箱 / 角色 / 创建邀请 / 复制邀请链接 | admin staff invitations (`/admin/invitations`; the one-time issuance surface) |
| 正在前往工作台… / 前往员工登录 | the staff-on-student-route redirect placeholder + its /me-failure exit (staff-root-redirect.spec pins the frame sequence; copy is the locator) |
| 页面不存在 / 返回首页 | the custom 404 (not-found.spec + the not-found SHOTS face) |
| 平台代码（创建后不可改） / 显示名 / 排序值 / 新建平台 / 创建平台 / 编辑 / 保存修改 | admin platform registry (`/admin/platforms`; platform-registry.spec pins the CRUD chain and the client-refusal zero-POST) |
| 任务评论 / 发布评论 / 发布回复 / 回复 | task-detail comment thread |
| 通知收件箱 / 标为已读 | notifications |
| 积分余额 (`getByLabel` + `[aria-label='积分余额']`) | student shell points readout |
| 我的附近 | rankings around-me |
| 操作原因 / 基础奖励积分 / 任务评分 | teacher task form, admin/moderation dialogs |
| 任务标题 / 任务描述 / 需要多少人完成 / 领取后截止（天） | the 2026-10-23 one-click create dialog (需要多少人完成 is the one required number, spec §3.1; the days field is the main-area deadline input) |
| 选择星级 (radiogroup) / 发布身份 (radiogroup) | review dialog, comment compose |

### Roles + accessible names

Buttons: 登录 · 注册 · 领取任务 · 开始上传 · 上传重试相关 · 兑换 · 确认兑换 ·
发布评论 · 发布回复 · 回复 · 赞 · 表情 🔥 · 举报 · 提交举报 · 标为已读 ·
完成 · 取消 · 创建草稿 · 保存修改 · 使用模板 (the optional schema's
starting-point filler — never a requirement) · 预览导入 · 重新上传文件 · 通过并发放奖励 ·
通过并扣减积分 · 退回修改 · 确认停用 · 确认绑定 · 确认揭示身份 ·
一键继续 (the unified login's conditional fast-login banner — layer 0,
owner feedback ③ 2026-10-17: the remembered-device probe's one-click leg,
now on /auth/challenge/remembered + the empty-body verify) ·
获取验证码 (the unified login's FIRST send — the code row with its
重新发送 cooldown appears only after it, layer 2's condition) ·
使用密码登录 / 使用验证码登录 (the unified login's weak link — the
password expansion's disclosure control, aria-expanded) ·
完成登录 (the unified login's TOTP leg, layer 2a) ·
发送验证码 / 绑定手机号 / 更换手机号 (the staff phone-binding flow,
PhoneBindSection) ·
代重置密码 / 踢出全部会话 / 提升为管理员 / 降级为教师 / 批量停用 / 批量恢复 /
确认执行 / 撤销 / 确认撤销 / 重发 (Wave 2a's admin power-pack: the user-detail
actions, the batch verbs, and the invitation lifecycle panel)

Checkboxes: 30 天内免登录 (the code form, the 2026-10-20 owner copy) · 记住此设备 30 天 — the password/totp seats ·
提前 24 小时提醒 / 提前 4 小时提醒 / 短信 / 邮件 / 站内通知 (the advanced
notification policy; OFF by default per the 2026-10-23 spec §3.1)

Buttons (batch 2): 切换到 <mask> / 从本机移除 <mask> (the recent-logins
cards' pick and device-local removal verbs — the aria-labels carry the
G11 mask, never the full address; the card FACE is the shared
`RecentLoginCardButton`, rendered by BOTH the login page's cards and
the shells' 切换账号 menu — the 2026-10-23 popover batch) ·
使用其他账号 (the switch menu's bottom verb — the direct-hop escape
hatch) · 免登录 (the multi-slot batch's chip: a menu card whose OWN
minted device_slot is still in the probe array — this pick lands
without a code; the mask plays no part, it collides across accounts —
the 2026-10-24 slot-matching contract) ·
（等 N 个） (the banner's multi-account note, component-level — not
e2e-asserted; the banner's zero-cookie face is what the baselines hold) · (the
code form's copy is audience-neutral; the password expansion's notes the
totp audience; the TOTP leg is where the meaning LIVES — the 2026-10-15
terminal ruling: the flag is sent whenever checked and the backend
routes it by audience, STUDENT verify honoring it, staff arms ignoring
it, the ADMIN totp leg issuing the cookie).

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
积分已发放。 · 注册成功，请使用学号登录。 (RETIRED with ④ going live — #100
made the register envelope the login, nothing routes to /login after
registration; removed in the post-merge cleanup) · 继续以 … 登录 (the
fast-login banner's masked-phone offer, owner feedback ③) · 该设备尚未记住
或已过期，请使用下方短信验证码登录。 (the banner's stale-click honest
fallback) · 手机号已绑定。 (the staff phone-binding success note, item ①) ·
恢复代码只显示这一次 · 已复制 · 动态口令绑定完成，员工账号已激活。 ·
已成功导入 1 个任务单元 · 请先设置完成人数（任务单元）再发布 (the zero-unit
publish precondition AND the server verdict's mapping — one copy) ·
未配置（宽松校验） (the optional-schema fact) · 已发布 · 草稿 · 已批准 · 待发放 · 已发放 · 已停用 ·
启用 · 正常 · 累计获得 · 可花费/当前可花费/消耗积分 · 匿名用户 · 暂无通知 ·
暂无附近排名 · 判无效会取消锁定的奖励与积分，学生需重新提交。审计记录不可删除。

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
| `.board-rows` · `.board-row` · `.board-rank` · `.board-me-tag` · `.board-honor` | rankings | me-tag asserts around-me anchoring; board-honor is the honor chip (renamed from `.honor-chip` in plan-13 with no spec uses) |
| `.reward-card` · `.balance-quiet` · `.reward-cta-note` | rewards list | balance-quiet is the wallet's 累计获得 value (re-anchored from a parent-hop locator in plan-13); reward-cta-note is the spend-state note under the CTA |
| `.notif-item` · `.notif-item[data-read='false']` · `.notif-title` | notifications | data-read asserts unread state |
| `.review-item` · `.review-pair` · `.review-tier` | teacher review queue; admin redemption queue | pair/tier assert VALIDATED tier pairing; the admin queue row is located by the run-derived item name (端到端奖励卡 + RUN[:6]) — NEVER by queue position (.first() would pick a residue world's oldest request) |
| `.import-preview` · `.moderation-key` · `.mono` | teacher import / moderation | |
| `dialog.dialog` | generic dialog scoping | retired at C3; the `getByRole("dialog")` replacement landed |
| `#submission-file` · `#assignment-import-file` | file inputs | keep as file-upload ids or replace with `getByLabel` of equal strength |
| `#task-schema` · `#task-schema-version` · `#task-target-count` · `#task-duration-days` · `input[name='assignment_id']` | task form fields | the 2026-10-23 one-click create adds the target-count and days inputs; the schema stays optional |
| `[data-assignments-list]` · `[data-user-id]` | teacher assignments list, admin user rows | |
| `.metric-row` · `.metric` | teacher task statistics (detail) | the 2026-10-23 总单元 metric joins the row; teacher.spec asserts the minted count here |
| `.app-shell[data-shell]` | student shell state marker | shell-refresh spec pins the chrome verdict per state (loading/workspace/anonymous/error/gate); the optimistic frame reuses the existing `.app-sidebar`/`.app-nav`/`.app-bottomnav` structural locators |
| `.task-card[data-depleted='true']` | depleted square card marker | same verdict as the 已被领完 badge (defect #20 / batch ④); the recede treatment assertions (dashed border, dimmed content) key on it |
| `[data-testid='invitation-link']` | admin one-time invitation link box | admin-invitations.spec pins the link text shape (origin + `/staff/invite/<urlsafe-token>`) and follows it to the accept page; CopyButton beside it is label-anchored (A-table) |
| `.meta-deadline` · `.reward-window` | zero-weight mask anchors | visual-suite mask hooks (shot-surfaces): the task card's clock-derived deadline line and the reward card's window line; the classes carry no styles by design |
| `.login-fast-hint` · `.login-fast-stale` | login fast-login banner (owner feedback ③) | the two-state pin's structural zero-element anchor (auth-extension-2: no cookie → count 0); the banner's own text/button assertions are the A-class pair |
| `.switch-menu` · `.switch-scrim` · `.switch-anchor` | the shells' 切换账号 popover (2026-10-23) | the menu's presence/absence is the empty-fallback pin (chains A/B assert count 0); the region carries aria-label 切换账号; a transient floating layer — deliberately outside the pixel baselines |
| `.switch-menu-fast` | the menu card's 免登录 chip (multi-slot batch, 2026-10-24) | chains C/D assert it on the remembered card; absent on unmatched cards |
| `[data-testid='mobile-login-totp-leg']` | staff mobile-code login's ADMIN second leg (item ①) | mirrors `email-login-totp-leg`; the leg's presence/absence is the TEACHER-vs-ADMIN branch pin |

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
