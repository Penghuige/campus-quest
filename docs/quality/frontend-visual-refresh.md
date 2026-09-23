# CampusQuest Visual Refresh Brief

> Status: post-E6 visual-only workstream.
>
> Branch: `design/visual-refresh-v1`
>
> Intent: materially improve visual quality **without changing product semantics, API contracts, routing, authorization, or business state machines**.

## 1. Why this refresh exists

The current frontend is functionally strong and accessibility-conscious, but visually it reads as a large collection of bordered white panels with similar hierarchy.

The main problems are not “wrong colors”. They are structural:

- almost every feature uses the same border + white surface + medium radius treatment;
- page titles, section titles, cards, status blocks, and operational tables compete at similar visual weight;
- the top navigation becomes crowded as Student / Teacher / Admin capabilities grow;
- Student pages do not feel meaningfully more motivating than Admin pages;
- Teacher/Admin pages are functional but lack a strong workstation hierarchy;
- there is little brand character beyond the primary blue;
- iconography and visual anchors are sparse, so users scan text before shape;
- the UI follows the old design system literally, but the result is visually safe rather than polished.

The refresh should **improve hierarchy and rhythm first**, then color, icons, and delight.

## 2. Target personality

Use these words when evaluating a visual choice:

**calm, polished, focused, academic, optimistic, compact, slightly playful**

Avoid:

**template-looking, card wall, neon, glassmorphism, enterprise-grey everywhere, cartoon skin, excessive gradients, giant KPI tiles**

CampusQuest should feel like a modern productivity product for students, not a school intranet and not a game.

## 3. Reference family

Use references for principles, not pixel copying.

### Linear — calm density and consistent hierarchy

Official references:

- https://linear.app/changelog/2026-03-12-ui-refresh
- https://linear.app/now/behind-the-latest-design-refresh

Borrow:

- dimmer navigation so the work surface wins;
- consistent page/header grammar;
- compact but readable spacing;
- quiet borders and stronger typographic hierarchy;
- predictable action placement.

Do **not** copy its dark-purple personality or developer-product vocabulary.

### Vercel Dashboard — navigation and operational scanability

Official references:

- https://vercel.com/changelog/new-dashboard-navigation-available
- https://vercel.com/changelog/dashboard-navigation-redesign-rollout
- https://vercel.com/blog/dashboard-redesign

Borrow:

- desktop sidebar for stable primary navigation;
- clear page context and local tabs;
- content-first work surface;
- mobile navigation designed deliberately rather than squeezed desktop nav;
- dense operational data with strong whitespace discipline.

### Notion — typography and low-friction reading

Official reference:

- https://www.notion.so/help/notion-for-web

Borrow:

- readable typography;
- restrained surface decoration;
- generous line-height where reading matters;
- quiet metadata;
- strong content grouping without wrapping every group in a card.

### Duolingo — motivation, used sparingly

Official references:

- https://blog.duolingo.com/how-streaks-keep-duolingo-learners-committed-to-their-language-goals/
- https://blog.duolingo.com/new-duolingo-home-screen-design/

Borrow only:

- compact progress cues;
- positive completion feedback;
- visible points/rank momentum;
- a clear “what should I do next?” path.

Do **not** introduce mascots, cartoon characters, bouncing animations, bright game gradients, or game-copy into serious workflows.

## 4. Visual architecture

### 4.1 Shared desktop shell

Move the primary desktop information architecture toward:

~~~text
┌──────────────┬──────────────────────────────────────────────┐
│ CampusQuest  │ Page context / breadcrumb        actions    │
│              ├──────────────────────────────────────────────┤
│ Home         │                                              │
│ Tasks        │             WORK SURFACE                     │
│ Claims       │                                              │
│ Ranking      │                                              │
│ Rewards      │                                              │
│ Community    │                                              │
│              │                                              │
│ Notifications│                                              │
│ Profile      │                                              │
└──────────────┴──────────────────────────────────────────────┘
~~~

Rules:

- sidebar is visually quieter than the content;
- current route has one obvious active state;
- role-specific shells share geometry but not every nav item;
- Teacher/Admin may use denser local subnavigation;
- do not duplicate global navigation inside page cards.

### 4.2 Mobile shell

For Student narrow view:

- top bar: product mark + context + notification/avatar;
- bottom navigation: 4–5 most-used destinations;
- overflow / profile holds secondary destinations;
- important task action stays near thumb reach;
- no horizontally wrapped desktop nav.

Teacher/Admin mobile can remain simplified list-first rather than attempting full workstation parity.

## 5. Color direction

Keep token-first OKLCH architecture.

Recommended visual change:

- background becomes slightly warmer / less blue-grey;
- surfaces should have **fewer visible borders**;
- introduce one soft “brand wash” token for hero/progress emphasis;
- primary remains cool blue/indigo, but use it with more restraint;
- use semantic colors only for actual meaning;
- rarity stays local.

Suggested semantic structure, not exact final values:

~~~text
background        warm-neutral / near-white
surface-1         white
surface-2         subtle neutral tint
surface-raised    white + small shadow
surface-brand     very low-chroma primary tint

primary           indigo-blue
accent            optional cyan / mint only for positive momentum
success           green
warning           amber
danger            red
~~~

Avoid full-width saturated gradients.

A very subtle gradient is acceptable only for Student progress / reward hero areas when text contrast remains AA.

## 6. Typography direction

The current system font stack is acceptable; do not add a webfont unless there is a measured reason.

Improve the hierarchy:

- page title: slightly larger and stronger;
- section eyebrow / metadata: smaller and quieter;
- body: keep comfortable Chinese line-height;
- numeric value: use tabular numerals and slightly stronger weight;
- avoid using font-size alone for hierarchy; combine weight, spacing, and grouping.

Recommended hierarchy:

~~~text
display / student hero     rare, not every page
page title                  strong
section title               medium strong
card/item title             medium
body                        normal
metadata                    quiet
overline/eyebrow            compact
~~~

## 7. Shape, border, and elevation

Current problem: too many panels visually say “I am a card”.

New rules:

- page sections do not need borders by default;
- cards are for discrete objects, not layout wrappers;
- use background contrast before border;
- most borders should be softer than current `--border`;
- only interactive/selected states need stronger keylines;
- slightly increase large-panel radius, but do not turn everything into pills;
- one raised shadow level is enough for dialog/popover/hero objects.

## 8. Iconography

Introduce a coherent icon language.

Preferred implementation order:

1. reuse existing semantic glyphs where already tested;
2. add a small local icon primitive set using simple stroke SVGs;
3. only add a third-party icon package if it clearly reduces maintenance.

If adding a dependency, `lucide-react` is acceptable, but do not replace text labels with icons.

Use icons for:

- navigation;
- task/reward metadata;
- notification categories;
- empty states;
- concise action affordances.

Do not decorate every label.

## 9. Student experience

Student pages should feel the most distinctive.

### Dashboard

Replace the current generic “page title + equal panels” feeling with:

~~~text
Good evening / 我的主页

┌────────────────────────────────────────────────────────┐
│ TODAY / 当前最重要                                    │
│ Task / revision / deadline + one primary next action   │
└────────────────────────────────────────────────────────┘

Points / next reward progress      Monthly rank / around me

Continue / My claims
Discover tasks

Recent notification / community activity
~~~

Priorities:

- the next action should visually dominate;
- points/rank are motivating support, not equal KPI tiles;
- deadline urgency is visible without using danger red too early;
- task cards should have stronger title/reward/deadline hierarchy.

### Task cards

Use:

- compact rarity accent;
- small platform/category icon;
- reward value as a clear supporting signal;
- deadline availability in one metadata row;
- hover/focus lift or keyline change;
- more breathing room between title and metadata.

Avoid four badges fighting for attention.

### Claim / Submission

Make the state machine visible as a clean vertical or horizontal progress timeline:

~~~text
领取任务 → 上传 → 自动校验 → 老师审核 → 完成
~~~

Current step strong; previous steps quiet; future steps subtle.

Validation failures should look like an actionable report, not a generic alert box.

### Rewards / ranking

Allow a little more personality:

- reward cards may use a soft illustration/icon tile;
- rank movement and point progress can use compact visual bars;
- top-three ranking may have restrained special treatment;
- never shame low-ranking users.

## 10. Teacher experience

Teacher is a workstation.

Prioritize:

- persistent navigation;
- task/review context;
- denser tables;
- clear filters;
- master/detail review split;
- strong selected-row state;
- actions aligned consistently.

Review queue should feel closer to Linear/Vercel operational tooling than Student cards.

Avoid decorative hero sections.

## 11. Admin experience

Admin should be the calmest and densest surface.

Use:

- sidebar;
- page-level title + description + primary action;
- filters/search in a stable toolbar;
- tables with restrained row hover;
- explicit destructive confirmations;
- drawers/dialogs for focused mutation;
- audit and repair pages should look serious.

Do not make Admin visually gamified.

## 12. Auth experience

Authentication should become more polished without being playful.

Recommended structure on wide view:

~~~text
┌───────────────────────┬───────────────────────────────┐
│ CampusQuest           │ login / registration card     │
│ academic task platform│                               │
│ subtle brand visual   │                               │
└───────────────────────┴───────────────────────────────┘
~~~

On narrow view collapse to a centered form.

The brand side may use a **small abstract campus/task illustration** later, but do not block the refresh on custom artwork.

## 13. Empty/loading/error states

Current primitives are technically correct but visually generic.

Refresh:

- skeletons should resemble the actual target surface;
- empty states may use one restrained line icon + direct action;
- errors should reserve red for the error message area, not tint entire large panels;
- request IDs remain quiet metadata.

## 14. Motion

Allow:

- 120–180 ms surface/opacity/transform transitions;
- 1–2 px hover lift on selected interactive cards;
- subtle progress fill;
- dialog/drawer transitions.

Do not add:

- springy page choreography;
- looping celebration;
- animated gradients;
- confetti for normal task completion.

Respect `prefers-reduced-motion`.

## 15. Minimal-change implementation contract

This PR is intentionally separated from Plan 10.

Visual work MUST NOT change:

- API request/response shapes;
- business error mapping;
- role/permission gates;
- auth/token flows;
- task/claim/submission state semantics;
- data-testid values used by E2E unless tests are updated solely for structural DOM changes;
- user-visible product wording unless the visual layout makes a small copy adjustment necessary.

Preferred change order:

1. tokens in `globals.css`;
2. shell/nav primitives;
3. shared UI primitives;
4. Student dashboard + task/claim surfaces;
5. ranking/rewards/community;
6. Teacher workstation;
7. Admin workstation;
8. auth polish.

Do not rewrite feature data logic while restyling.

## 16. Merge strategy

This branch starts from current `main` while PR #6 / Plan 10 is still finishing.

Rules:

1. keep this PR Draft until Plan 10 merges;
2. before implementation, rebase/merge latest `main` into this branch;
3. resolve E6 frontend changes first;
4. keep visual commits separate by surface;
5. rerun the final release gate after visual implementation;
6. merge only after visual regression review.

This keeps the later integration close to “CSS + shell + presentation composition” rather than business-code conflict resolution.

## 17. Visual acceptance checklist

Before declaring the visual refresh complete, capture screenshots for:

### Student
- dashboard desktop + 375x812;
- task list/detail;
- claim/submission validation error;
- rewards;
- rankings around-me;
- community;
- notifications.

### Teacher
- review queue desktop;
- task management table;
- task creation/import dialog;
- narrow fallback.

### Admin
- user table;
- rewards/redemptions;
- audit;
- system settings;
- destructive confirmation.

### Auth
- student login/register;
- staff login;
- TOTP setup;
- mobile.

For each key screen inspect:

- hierarchy in grayscale;
- keyboard focus;
- 200% zoom;
- long Chinese text;
- loading/empty/error;
- dark text contrast;
- no layout shift from large point/rank values.

## 18. Definition of done

The refresh is complete when:

- the product no longer looks like a uniform card-and-border template;
- Student, Teacher, and Admin surfaces have distinct density/purpose while sharing one design language;
- desktop navigation scales cleanly;
- narrow Student navigation is intentional;
- no domain behavior changed;
- E2E selectors remain stable or are deliberately updated;
- typecheck/lint/unit/build/Playwright pass;
- fresh `make release-gate` passes after the final visual commit.
