# CampusQuest Quality Gates

> Every implementation Agent and reviewer uses the relevant sections before marking work complete.

## 1. Universal gate

A change is not ready if any applicable item fails.

### Specification

- The change implements the approved spec and plan behavior.
- No product semantic was invented silently.
- No unrelated feature or refactor entered the diff.
- Stable public API and error names match project contracts.

### Tests

- New behavior has tests.
- A bug fix has a regression test that failed before the fix.
- Focused tests pass.
- Relevant module or integration gate passes.
- High-risk concurrency and idempotency behavior is tested on real PostgreSQL where applicable.

### Code quality

- Formatter passes.
- Linter passes.
- Type check passes for touched project code.
- No secrets, debug statements, or temporary files.
- No unresolved placeholder markers for required behavior.
- No broad ignored errors added merely to silence tooling.

### Reviewability

- Names explain intent.
- Non-obvious locking and transaction decisions have a short WHY comment or a test documenting them.
- Generated code was reviewed.
- Diff is small enough to review; otherwise split it.

## 2. Frontend visual gate

Review the changed flow at:

- narrow viewport;
- desktop or wide viewport;
- normal state;
- loading;
- empty where applicable;
- error;
- permission denied where applicable;
- long content.

Check:

- visual hierarchy has one obvious primary action;
- page does not become a wall of equal cards;
- spacing follows shared rhythm;
- tokens are used instead of arbitrary repeated values;
- rarity colors remain accents;
- destructive actions are visually distinct and confirmed;
- dense tables remain scan-friendly;
- mobile actions remain usable;
- status text is understandable without color.

For a new core page, capture screenshot evidence in the review or PR workflow when tooling supports it.

## 3. Frontend accessibility gate

Keyboard-only pass:

- reach all interactive controls;
- visible focus;
- logical focus order;
- dialog open and close restores focus appropriately;
- menus and tabs follow primitive behavior;
- no keyboard trap.

Content pass:

- icon-only buttons have names;
- form labels and errors are associated;
- color is not the sole state indicator;
- error and success updates are announced when focus does not naturally convey them;
- reduced motion is respected;
- touch targets are comfortably usable;
- contrast is acceptable for body, muted, and status text.

Prefer automated accessibility tests for stable screens, but automated tests do not replace a short keyboard review.

## 4. Frontend privacy gate

Inspect both API response shape and rendered DOM.

Student and public pages MUST NOT unnecessarily expose:

- student number;
- phone;
- email;
- raw object-storage key;
- private internal identifier;
- anonymous comment identity.

Do not accept hidden-by-CSS as privacy.

Anonymous Admin reveal remains an explicit action.

## 5. Frontend performance gate

Before optimizing, check high-impact issues:

- avoidable sequential data fetching;
- oversized Client Component boundary;
- accidentally bundled heavy library;
- repeated server requests for identical data;
- unbounded large table or list rendering;
- unstable provider or state causing broad rerenders.

Do not block a correct feature on speculative micro-optimization.

For a large new page, use the Vercel React best-practices skill or reference during review.

## 6. Backend architecture gate

Check dependency direction:

router -> service/use case -> repository/query/port -> adapter/database

Reject if:

- router directly performs a multi-table business transaction;
- worker contains alternate domain rules;
- feature reaches into another domain's ORM internals instead of a contract or port where isolation is required;
- one generic manager or service accumulates unrelated behavior;
- response schema exposes persistence or private fields accidentally.

## 7. Backend correctness gate

For each state-changing use case, identify:

- invariant;
- transaction boundary;
- rows or resources locked;
- database constraint fallback;
- expected conflict error;
- retry and idempotency behavior.

If these cannot be stated clearly, the code is not ready.

## 8. Database gate

For schema changes:

- Alembic migration exists;
- clean upgrade to head succeeds;
- relevant downgrade strategy is understood;
- constraint and index names are explicit;
- new hot query has appropriate indexes;
- migration does not depend on network or runtime service code.

For concurrency:

- use real PostgreSQL;
- test two or more independent transactions;
- repeat race-prone tests enough to expose flakiness.

## 9. Worker and external-system gate

For each job:

- repeated same job is safe;
- permanent and temporary provider failures are distinguished;
- retries are bounded;
- unknown external outcome uses a provider idempotency key where possible;
- job does not create duplicate business side effects;
- job calls domain service rather than editing business tables ad hoc.

## 10. Security gate

Authentication and authorization:

- state-changing route checks active account;
- resource ownership and permission are checked server-side;
- Teacher access is Task-scoped;
- Admin-sensitive action has required reason and audit where specified;
- CSRF protections remain intact for cookie-authenticated mutations.

Secrets and logging:

- no password, OTP, token, TOTP secret, or recovery code in logs or errors;
- contact information is masked where unnecessary;
- object URLs are authorized before generation.

Files:

- declared extension is not trusted;
- parser limits are enforced;
- SQLite remains read-only and query-only;
- XLSX and ZIP limits are checked;
- file content does not enter application logs.

## 11. Points and reward gate

Any change touching points or rewards must prove:

- original Ledger rows are immutable;
- idempotent source key prevents duplicate assignment reward;
- wallet and reservation update is transactional;
- spendable points never go negative;
- limited stock never oversells;
- normal redemption does not reduce historical ranking contribution;
- reversal corrects the intended historical period;
- retry does not double-update Redis ranking.

## 12. Task, Claim, and Submission gate

Any change touching these states must prove:

- Assignment is not double-allocated;
- same user and Task active rule is preserved;
- three-actionable-Claim quota is preserved;
- reward deadline boundary exactness is preserved;
- valid submission cannot race into erroneous expiry;
- Teacher review latency does not lower reward;
- revision-window semantics are preserved;
- history is not overwritten.

## 13. Community gate

- anonymous DTO cannot leak identity;
- parent belongs to the same Task;
- edit history is retained;
- soft-delete preserves child context;
- vote and reaction uniqueness is preserved;
- rate limit prevents basic spam;
- task rating requires a completed Claim;
- explicit Admin identity reveal is audited.

## 14. Completion evidence

An Agent's final implementation report should include:

- files or area changed;
- tests added;
- exact verification commands run;
- pass or exit result;
- screenshots or visual notes for frontend changes where relevant;
- known limitations that are explicitly outside scope.

"Looks good" is not verification evidence.

## 15. Release gate

V1 release is governed by the dedicated E2E hardening plan and eventual command:

~~~bash
make release-gate
~~~

A fresh zero-failure run is required after the final release-gate fix.

Until that command exists, use the strongest currently implemented subset rather than pretending the future gate has run.
