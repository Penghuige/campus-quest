# CampusQuest Frontend Patterns

> This document describes implementation patterns. Visual styling belongs in frontend-design-system.md.

## 1. Core frontend architecture

Preferred feature structure:

~~~text
frontend/src/
├── app/
├── components/
│   └── ui/                 # shared low-level primitives
├── features/
│   ├── auth/
│   ├── tasks/
│   ├── submissions/
│   ├── rewards/
│   ├── rankings/
│   ├── community/
│   ├── notifications/
│   └── admin/
└── lib/
    ├── api/
    ├── auth/
    ├── time/
    └── formatting/
~~~

Feature components own product composition. components/ui owns generic primitives.

Do not put product logic into copied shadcn primitive files.

## 2. Server/client boundary

Default to Server Components for:

- page shell;
- initial authenticated data;
- read-heavy task and detail views;
- metadata.

Use Client Components when interaction requires it:

- file input and upload progress;
- dialogs and drawers with local interaction;
- vote or reaction optimistic feedback;
- complex filters controlled in URL or client state;
- forms requiring live state.

Do not add use client to a large page because one small child is interactive. Isolate the interactive child.

Before implementing a Next.js behavior, read the installed version's docs under node_modules/next/dist/docs/.

## 3. API ownership

The frontend consumes typed API contracts.

Never duplicate backend business calculations for:

- reward tier;
- current reward points;
- claim quota;
- task eligibility;
- redemption eligibility;
- ranking score;
- anonymous identity rules;
- review permissions.

Frontend may derive presentation-only values such as countdown text from a server timestamp, but the result is never authoritative.

At important boundaries, refetch the server state.

## 4. State categories

Classify state before choosing a library or hook.

### URL state

Use URL and search params for:

- table page;
- sort;
- filter;
- search;
- ranking period;
- shareable tabs.

### Server state

Use the selected server-state/query approach for:

- tasks;
- claims;
- validation reports;
- points;
- leaderboard;
- comments;
- notifications.

### Form state

Keep scoped to the form. Avoid pushing every input into global state.

### Ephemeral UI state

Dialog open, local selection, expanded thread, hover, focus, and temporary optimistic reaction state.

Global state is the exception, not the default.

## 5. Data fetching

Avoid waterfalls.

Bad:

~~~text
load page
  -> fetch user
     -> fetch claim
        -> fetch task
           -> fetch points
~~~

Prefer parallel fetching when dependencies are independent, and server aggregation endpoints when the UI always needs the same combined shape.

Do not create a frontend backend-for-backend layer that re-implements domain logic. Server-side Next.js code may coordinate presentation queries but not mutate CampusQuest business state independently.

## 6. Forms and mutations

Use a consistent mutation flow:

1. user input;
2. client convenience validation;
3. submit;
4. backend validation and business decision;
5. render stable business error by code;
6. update or refetch affected server state;
7. focus or announce result where appropriate.

Client validation improves feedback. It never replaces backend validation.

Map business errors by error.code, not by parsing Chinese text.

## 7. Optimistic updates

Allowed selectively for reversible, low-risk interactions:

- vote;
- emoji reaction;
- read or unread notification;
- local comment edit display after accepted mutation.

Avoid optimistic updates for:

- claim allocation;
- abandon;
- file validation;
- submission approval;
- points;
- redemption;
- account status;
- system settings.

For these, wait for authoritative server confirmation.

## 8. Page archetypes

### Student Dashboard

~~~text
Page title / greeting
┌ 当前最重要 (next-action hero — the ONE dominant element) ┐
│ revision > earliest-deadline claim > discovery CTA        │
└───────────────────────────────────────────────────────────┘
Points + next reward progress     Month rank
Growth summary                    Around-me preview

Available tasks (deduped against open claims)
Recent notifications preview
~~~

Do not make six equal KPI cards the first screen. The hero is the
only page-level `--surface-brand` consumer (Plan 11).

### Task detail

~~~text
title + rarity + rating
任务说明 / requirements          reward / deadline / availability
                                [领取任务]
community thread (hairline-separated groups, one indent rail)
rating section (sibling on the same 56rem view step)
~~~

The decision information precedes the claim action (Plan 11 owner
review P1): source order is the narrow reading order (title →
description → facts → CTA); ≥48rem places the decision column beside
the description. Before claim, Assignment payloads are hidden.

### Claim and Submission detail

~~~text
task title + status badge + five-step progress strip
[revision banner FIRST when a revision is required]
assignment identity panel (NEUTRAL surface — identity ≠ status)
deadline + current backend reward
status timeline (the strip)
upload/version area (one stateful workflow surface)
validation report
review/revision note
secondary abandon action when allowed
~~~

The neutral assignment panel and the progress strip are Plan 11
rulings; the validation report should prioritize errors first, then
warnings, then successful checks.

### Rewards

~~~text
balance hero: ONE dominant spendable stat on the page ground
  + quiet earned/frozen metadata line + next-reward goal rail
reward shelf grid — tiles: identity chip + name lead, cost is the
  primary signal, stock/window quiet metadata, CTA carries availability
recent redemptions
~~~

The spendable figure gates redemption, so it owns the hierarchy — no
equal-metric balance row (design-system §9 metrics).

### Rankings

~~~text
segmented period tabs (Daily | Monthly | All)

Top list — restrained top-3 (numeric weight + one subtle keyline);
current-user anchor row wherever the caller appears

----------------

Around me — panel-wrapped as one object; the neighborhood lead
renders only when the caller is outside the top list
~~~

Keep current-user highlight consistent.

### Notifications

~~~text
filter tabs (segmented) + unread count badge
inbox rows on the page ground: unread raised + weighted, read quiet;
kind keyline on the glyph tile, outcome on the semantic tint;
body clamped, per-row action link
~~~

### Teacher review

Desktop may use master/detail:

~~~text
filters + queue
┌───────────────┬──────────────────────────┐
│ submissions   │ selected submission      │
│ list          │ validation / preview     │
│               │ history / approve/revise │
└───────────────┴──────────────────────────┘
~~~

On narrow layouts, split into list -> detail navigation.

### Admin operations

Use data table + filter + explicit action drawer/dialog.

Do not render an admin page as dozens of cards if the real work is scanning records.

## 9. Data table pattern

A reusable data-table layer may provide:

- server pagination;
- sorting;
- URL-synced filters;
- column visibility;
- row selection only where batch actions exist;
- loading, empty, and error states;
- responsive fallback.

Do not build one god-table with every possible feature. Compose per page.

Batch destructive operations require explicit selection count and confirmation.

## 10. Dialog and drawer pattern

Use dialogs for:

- short focused confirmation;
- sensitive action requiring reason;
- small forms.

Use drawers or sheets when:

- preserving list context is valuable;
- detail is longer but not a full workflow.

Use full page when:

- upload or review flow is complex;
- deep linking matters;
- user may spend significant time there.

Avoid nested modals.

The implementation is `components/ui/dialog` (shadcn-pattern Radix):
every product dialog renders through it, so role/labelledby wiring,
focus trap, Escape, and outside-click semantics live in one place and
e2e locates dialogs by `getByRole("dialog", { name })`. Hand-rolled
native `<dialog>` / `showModal()` is forbidden for new code;
StaffMenuSheet's transient navigation menu is the one sanctioned
exception (kept native by the plan-14 ledger ruling; self-contained
on `.app-menusheet*` since the C3 retirement).
Busy-submitting dialogs guard asynchronous closes in `onOpenChange`
rather than disabling the platform's Escape handling.

## 11. File upload pattern

State model:

~~~text
idle
-> preparing upload intent
-> uploading
-> finalizing
-> validating
-> validation_failed | under_review
-> revision_required
-> uploading new version
-> completed
~~~

Display actual state names in human language.

If upload to object storage succeeds but finalization fails, provide a retry-finalize path where safe rather than forcing a full re-upload.

## 12. Comments pattern

A comment component should not receive the full private User record.

Prefer a public author shape:

~~~ts
type PublicCommentAuthor =
  | { kind: "named"; nickname: string; honor?: string | null }
  | { kind: "anonymous"; label: string };
~~~

This makes privacy leakage harder by construction.

Moderation UIs use a separate moderation DTO.

## 13. Sensitive data pattern

Do not load data into the browser just in case and hide it with CSS.

If Student UI does not need student number, phone, email, or internal ID, the API response for that view should not contain it.

Anonymous identity is revealed through a dedicated Admin action only.

## 14. Dates and time

Centralize formatting.

Use:

- backend UTC timestamps;
- configured display and business timezone;
- absolute time plus relative text where useful.

Example:

9 月 19 日 18:00 · 还剩 3 小时 12 分

At deadline boundary, refetch authoritative Claim state.

Do not implement timezone arithmetic via hardcoded +8.

## 15. Error mapping

Maintain a shared mapping for common business codes:

~~~text
ASSIGNMENT_LIMIT_REACHED
NO_ASSIGNMENT_AVAILABLE
CLAIM_CUTOFF_REACHED
SUBMISSION_WINDOW_CLOSED
FILE_TYPE_NOT_ALLOWED
INSUFFICIENT_POINTS
REWARD_OUT_OF_STOCK
RATING_NOT_ELIGIBLE
ACCOUNT_NOT_ACTIVE
~~~

Unknown error:

- show a safe fallback;
- include request ID;
- log to observability;
- never show raw exception detail.

## 16. Component reuse rule

Before creating a component:

1. check components/ui;
2. check feature-local components;
3. check shadcn registry and docs;
4. create a new component only if semantics are genuinely new.

A component is not reusable merely because it has many props. Prefer small semantic composition over universal mega-components.

## 17. Thin-world visual evidence (dev gallery)

The e2e world seeds a thin fixture set, so many presentation states
(top-3 boards, CTA variants, populated threads, an unread inbox) can
never render live. Compose those states in `/dev/gallery` with
deterministic fixtures (real feature components, fixed timestamps) and
let the pixel baseline guard them — do NOT fatten world seeding for
presentation evidence.

Rules:

- gallery fixtures render the REAL row/tile components; duplicating
  markup in the gallery is banned (it drifts);
- when a contract has no read path for a state (vote/reaction
  stance), an optional gallery-documented fixture-seed prop beats
  markup duplication; the app's state source stays singular;
- an empty-world surface pins the deterministic EMPTY state as its
  pixel baseline (`masks: []` — an inert mask fails the mask
  engagement contract) plus a probe note naming the mask the shot
  must gain if the world ever seeds rows.

## 18. Performance patterns

Use the Vercel React best-practices skill or reference for implementation review.

High-priority concerns:

- eliminate avoidable async waterfalls;
- keep Client Component boundaries narrow;
- avoid shipping large libraries for tiny utilities;
- lazy-load genuinely heavy optional UI;
- do not create unstable provider values or rerender storms;
- virtualize only when data size justifies it;
- use image and font facilities appropriate to the installed Next.js version.

Performance work must be measured or tied to a known pattern, not cargo-cult memoization.

## 19. Accessibility patterns

Prefer tested primitives for:

- Dialog
- AlertDialog
- DropdownMenu
- Popover
- Tabs
- Tooltip

Do not replace native button, link, or input semantics with clickable divs.

When mutation succeeds or fails in a way not obvious from focus movement, provide an accessible announcement.

## 20. Pattern anti-list

Do not introduce:

- bespoke modal or focus trap when a Radix or shadcn primitive exists;
- global Zustand or Redux store for ordinary server data;
- custom fetch wrapper per feature;
- business enums duplicated with different names;
- raw API error-message parsing;
- CSS values that bypass design tokens without reason;
- a giant utils.ts or components.tsx;
- an Admin page that fetches all records and filters only in browser;
- anonymous DTOs containing hidden private fields.
