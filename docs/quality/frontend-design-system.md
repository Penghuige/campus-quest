# CampusQuest Frontend Design System

> Purpose: give frontend and design agents one stable visual language across Student, Teacher, and Admin surfaces.
>
> Product direction: **academic productivity with restrained gamification**.

## 1. Design intent

CampusQuest should feel credible enough for teachers and administrators, motivating enough for students, and dense enough for real operational work.

The interface should communicate:

- **trust** — academic and administrative actions feel deliberate and auditable;
- **clarity** — task status, deadlines, points, and next actions are obvious;
- **momentum** — progress, ranking, honors, and rarity add energy without turning the product into a game skin;
- **calm density** — dashboards and review queues can show substantial information without visual noise.

The preferred reference family is modern productivity software and well-composed shadcn/Radix dashboards. Do not imitate any reference brand one-for-one.

## 2. Visual personality

Use these adjectives when deciding between two visual treatments:

**calm, precise, compact, optimistic, human, slightly playful**

Avoid:

**flashy, casino-like, neon, glassy, futuristic-AI, overly corporate, cartoon-heavy**

Rarity labels and honors may be playful. Authentication, submission, review, account security, redemption approval, and Admin operations should remain visually serious.

## 3. Token-first implementation

All recurring visual values MUST originate from shared tokens, preferably CSS variables consumed by Tailwind and shadcn primitives.

Do not scatter raw hex values or one-off radius and shadow values through feature components.

Recommended token groups:

~~~text
surface:
  background          (warm-neutral near-white, Plan 11)
  surface-1
  surface-2
  surface-brand       (the ONE soft brand wash: hero/progress emphasis
                       only — never a page background)
  overlay / overlay-scrim

text:
  foreground
  muted-foreground
  subtle-foreground
  inverse-foreground

border:
  border              (soft; background contrast carries hierarchy first)
  border-strong
  focus-ring

semantic:
  primary
  success
  warning
  danger
  info

rarity:
  rarity-normal
  rarity-rare
  rarity-epic
  rarity-legendary

shape:
  radius-sm
  radius-md
  radius-lg

shadow:
  shadow-xs
  shadow-sm
  shadow-raised        (the one lifted level: dialog/popover/hero objects)

motion:
  duration-fast / duration-slow
  ease-standard
  lift-hover           (the 1–2px hover lift interactive cards may take)
~~~

Prefer OKLCH-compatible theme variables when the selected Tailwind and shadcn setup supports them.

### Token usage rules

- Primary accent is for the main action and current navigation state, not every icon.
- Semantic colors communicate actual state, not decoration.
- Rarity colors are accents on badge, icon, or keyline. They MUST NOT become full-page backgrounds.
- Border and surface contrast should carry most hierarchy; shadow is secondary.
- One page should not invent a new visual vocabulary.

## 4. Color strategy

Default experience should be light-first, with tokens structured so a complete dark theme remains possible.

**Identity ruling (Plan 11 direction shoot-out, owner pick "ink")**: the product's identity is a DARK navigation rail (sidebar / mobile bottom nav, `oklch(21% 0.018 262)`) beside warm light content — the rail is quiet in weight but bold in tone. The primary is `oklch(50% 0.18 264)` (#2b59c8), one notch more saturated than V1 so it carries the dark rail; display type (page titles, hero, metric numerals) rides the display rung (`--text-2xl` / 1.625rem) — scale contrast is the product's typographic signature.

Recommended behavior:

- large page backgrounds: neutral;
- cards and panels: one subtle surface step above background;
- primary action: one clear accent;
- destructive action: semantic danger only;
- success: completed or approved states;
- warning: deadline risk, revision needed, provider failure;
- info: neutral system information.

Rarity mapping should remain recognizable but restrained:

| Rarity | Intended accent |
| --- | --- |
| Normal | neutral or graphite |
| Rare | cool blue |
| Epic | violet |
| Legendary | warm amber or gold |

Do not rely on rarity color alone. Always render the text label.

## 5. Typography

The product is information-heavy. Body typography must prioritize readability over personality.

Use:

- a highly legible sans-serif body and UI font;
- optional restrained display treatment for product or marketing headings only;
- tabular numbers for points, ranks, dates, counts, and table metrics where alignment matters.

Hierarchy guideline:

- page title: strong but not oversized;
- section title: distinct from card titles;
- body: comfortable line height;
- metadata: muted, never so faint that it harms readability;
- dense table text: compact, but not uncomfortably small.

Avoid giant dashboard headings and oversized KPI numbers that push useful information below the fold.

## 6. Spacing and density

Use a consistent spacing scale. Prefer fewer spacing values rather than arbitrary per-component tuning.

Desktop:

- main content gutters clearly separate navigation and work area;
- table and review pages use a denser vertical rhythm than auth pages;
- related controls stay visually grouped.

Mobile:

- keep a comfortable page gutter;
- do not compress tappable actions into tiny icon-only clusters;
- stack primary controls before secondary metadata.

A page with operational data should prioritize the work, not empty whitespace.

## 7. Shape and elevation

Recommended character:

- controls: small-to-medium radius;
- cards: medium radius;
- dialogs and drawers: slightly stronger radius;
- pills: only for badges, statuses, and filter chips.

Use elevation sparingly:

- most cards use border plus surface difference;
- dialogs and popovers may use a shadow;
- avoid making every card look like it is floating.

## 8. Page shells

### Navigation shell (Plan 11: three bands)

All authenticated workspaces share one navigation geometry, three
bands by viewport:

- **Wide (≥64rem)**: a visually QUIET sidebar owns the primary
  navigation — the INK dark rail (light links, brand-tinted active
  state with an inset keyline; exactly one `aria-current="page"` per
  nav landmark); the top bar degrades to a context/actions strip
  (bell, user — no nav, no brand).
- **Medium (40–64rem)**: the horizontal top nav keeps every
  destination.
- **Narrow (<40rem)**: Student gets a fixed 5-slot bottom nav — the
  same INK dark rail — with safe-area inset and reserved content
  bottom padding (the fixed bar never covers a primary action);
  Teacher/Admin get a hamburger menu sheet carrying the FULL staff
  list (native dialog semantics: focus trap, Escape, backdrop close).

Routes never change with the band; reachability does not either.

### Student

Primary navigation should make these easy to reach:

- Home
- Tasks
- My Claims (anchored on the dashboard in V1 — no /claims index route)
- Rankings
- Rewards
- Notifications
- Profile

Dashboard priorities:

1. work requiring action;
2. approaching deadlines and revision requests;
3. points and reward progress;
4. ranking and growth;
5. task discovery.

Do not make the Student home a wall of equal-sized metric cards.
Plan 11 realizes this as ONE next-action hero (the single dominant
element, and the only page-level `--surface-brand` consumer): a
teacher-returned revision outranks everything, else the
earliest-deadline open claim, else a discovery CTA. Discovery cards
dedupe against the student's open claims.

### Teacher

Prioritize:

- review queue;
- task management;
- task statistics;
- assignment import;
- community and reports.

Teacher pages may be denser than Student pages.

### Admin

Prioritize operational scanability:

- users and whitelist;
- rewards and redemptions;
- system and notifications;
- audit;
- repair operations.

Admin pages should favor tables, filters, drawers, and explicit confirmations over decorative dashboard cards.

## 9. Component rules

### Buttons

Use a clear hierarchy:

- Primary: one main action in a local context.
- Secondary: normal alternative.
- Ghost: lightweight toolbar action.
- Destructive: destructive intent only.

A button is a button whatever the element: link-styled buttons
(`<Link className="btn">`) never render the anchor underline; inline
prose links (`.link`) keep theirs.

Avoid two adjacent primary buttons competing for attention.

Icon-only buttons require accessible labels and, where helpful, tooltips.

### Cards

Cards are grouping containers, not the default layout primitive.

Good uses:

- active Claim summary;
- reward item;
- compact growth summary;
- task card in discovery.

Poor uses:

- wrapping every section inside another card;
- turning every desktop table row into a large card;
- one card per label/value pair.

Plan 11 task cards: the title owns the card; rarity is a compact
LEFT KEYLINE (`data-rarity` border color — quiet for NORMAL) with
its TEXT label in the metadata row; color never carries rarity
alone. Interactive cards may take the 1px hover lift
(`--lift-hover`, reduced-motion safe).

### Identity vs status (Plan 11 core rule)

Assignment identity (platform / keyword / claim time) is NEVER drawn
in a semantic tone: `.claim-panel` renders on the neutral
`--surface-brand` surface with a neutral border in every state.
Workflow status lives only on the status badge, the five-step
progress strip (领取 → 提交 → 校验 → 审核 → 完成; current strong,
done quiet, future subtle; non-linear terminals render no strip),
and the alerts. When a revision is required, the revision banner
precedes the assignment facts and owns the visual priority.

### Tables

Use tables for Teacher and Admin dense data.

Requirements:

- sticky header when useful;
- sensible loading, empty, and error states;
- server-backed pagination and filters where data can grow;
- consistent row actions;
- destructive actions never hidden behind ambiguous unlabeled icons;
- numeric columns align predictably.

On mobile, use a deliberate compact/list alternative if a table becomes unreadable.

### Status badges

Status always includes text. Color is supplemental.

Use product wording such as:

- 待提交
- 校验中
- 待审核
- 需修改
- 已完成
- 已过期

Do not expose raw enum names such as UNDER_REVIEW.

### Task rarity

Rarity treatment:

- compact badge;
- optional small icon or edge accent;
- never outrank task title;
- no animated glow for Legendary.

### Forms

- label remains visible after input;
- help text explains consequences, not the obvious field name;
- errors appear close to the field;
- sensitive forms state what happens next;
- use appropriate autocomplete and input mode;
- disabled states remain legible.

### File upload

Upload UI must show:

- allowed formats;
- size policy;
- current file;
- progress;
- upload and finalize failures;
- validation state;
- structured validation result;
- retry or new-version action.

Drag-and-drop is optional enhancement; keyboard-accessible file selection is required.

### Leaderboard

Leaderboard should feel competitive but not humiliating.

Always provide:

- top results;
- around-me section;
- current-user highlight;
- period switch;
- honor display.

Do not use danger styling for low rank.

### Comments

Anonymous mode must be explicit before posting.

Comment UI should visually separate:

- author or anonymous label;
- timestamp and edited marker;
- content;
- vote and reaction controls;
- moderation or deleted state.

Deleted parent comments remain as tombstones so child context survives.

## 10. Loading, empty, error, and permission states

Every feature page MUST define these states before implementation is considered complete.

### Loading

- use skeletons where shape is stable;
- use inline spinner for isolated actions;
- do not block the whole page for a minor mutation.

### Empty

Explain:

1. what is empty;
2. why it might be empty;
3. what the user can do.

### Error

Show:

- human-readable message;
- retry when safe;
- request ID on unexpected server failure.

Do not expose stack traces.

### Permission denied

Do not render a broken page full of disabled controls. Explain that the user lacks access and provide navigation back.

## 11. Motion

Motion is subordinate to comprehension.

**Motion spec (review round 3 — tokens live in globals.css):**

- entrances: ONE orchestrated rise (opacity + 8px, `--motion-in` 220ms,
  `--ease-out-soft` settle) when a section's data mounts; lists stagger
  `--stagger` 40ms per child, capped at three children so long lists
  never crawl. Nothing animates longer than ~300ms — this is a
  productivity tool, not a showcase;
- only `transform` and `opacity` animate (GPU-composited; never
  width/height/padding/margins);
- skeletons are a LOW-CONTRAST directional sweep (`cq-sweep`,
  transform-only) — never an opacity blink;
- `prefers-reduced-motion` means FEWER and GENTLER animations, not
  zero: entrances become pure fades, movement and sweeps drop, color
  transitions stay.

Use motion for:

- the section/list entrance described above;
- drawer and dialog transitions;
- small list insertion or removal;
- optimistic vote and reaction feedback;
- subtle progress and state changes.

Avoid:

- page-load choreography on operational pages beyond the single
  section entrance;
- parallax;
- decorative looping animation (the skeleton sweep is the only loop);
- flashing rarity effects.

## 12. Accessibility baseline

Target WCAG 2.2 AA behavior for V1.

Requirements:

- semantic HTML first;
- visible keyboard focus;
- keyboard-operable controls;
- dialogs and popovers manage focus correctly;
- icons have accessible names where needed;
- form controls have labels;
- errors are programmatically associated;
- color is never the only state indicator;
- reduced-motion preference is respected;
- pointer and touch targets are comfortably usable, with primary mobile actions around 44px minimum target dimension when feasible;
- contrast is checked in both normal and muted states.

Prefer accessible primitives, for example Radix-backed shadcn components, instead of reimplementing dialogs, menus, popovers, tabs, and focus traps.

## 13. Responsive behavior

Design from workload classes rather than device names.

### Narrow

Student phone use. Prioritize current action and summary. Collapse secondary metadata.

### Medium

Tablet or small laptop. Sidebar may collapse; tables simplify.

### Wide

Teacher and Admin workstation. Use denser tables, split panes, filters, and persistent navigation.

Do not make desktop simply the mobile layout with more whitespace.

## 14. Copy and tone

CampusQuest copy should be concise, calm, and action-oriented.

Prefer:

- 当前可获得 160 积分
- 提交未通过校验，请修正 3 项问题
- 老师已退回修改，奖励档位已保留

Avoid:

- punitive framing such as 你被扣除 40 分;
- game slang in serious review or security flows;
- vague errors such as 操作失败;
- fake urgency.

## 15. Figma workflow

Figma is an optional design workspace, not the runtime source of truth.

Recommended reference frames:

1. Student Dashboard
2. Task Detail and Claim
3. Submission and Validation Report
4. Rankings and Around Me
5. Teacher Review Queue and Review Detail
6. Admin Dense Table and Confirmation Dialog

Figma components and code components should share names where practical.

Design tokens ultimately live in code. If Figma and repository tokens disagree, resolve the intended source deliberately rather than allowing silent drift.

## 16. Visual review checklist

Before frontend work is accepted, inspect at minimum:

- desktop and narrow viewport;
- normal, loading, empty, and error;
- long Chinese nickname and task title;
- long keyword;
- zero and very large point values;
- deadline under 4h;
- revision state;
- keyboard focus order;
- no sensitive identity leakage.

See docs/quality/quality-gates.md for the full gate.
