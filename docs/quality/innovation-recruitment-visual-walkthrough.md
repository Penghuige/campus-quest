# R3 recruitment visual walkthrough

2026-10-11. Approved R3 spec and frontend design tokens govern these changes.

Eight new Linux pinned-font screens cover the owner editor, mobile publication consent, mobile public detail, deliberate authorized contact read, public project with no approved achievement, closed detail, filtered empty list and unavailable detail. Functional tests separately exercise 120-character unbroken titles, 2000-character introduction, the exact UTC conversion of a dated publication, Escape/opener focus and successful-operation result focus. Axe must return zero violations; screenshot tolerance remains 0.01.

Only two existing R1 baselines change: the campus landing now includes independent recruitment after achievements, and the mobile navigation screenshot sees that changed landing behind its unchanged menu. Before/after mobile images were inspected. The first comparison failed on the changed mobile background (4180 pixels, ratio 0.02); this is the approved product addition. Profile-bottom logout and logout-confirmation baselines remain the original files. The existing project-draft form baseline compared successfully without regeneration. R2 achievement screenshots are compared, not replaced.

The new error/loading detail states carry a level-one heading. Tests scope product errors to main, because Next's own empty alert exists before authenticated content. Dialog checks wait for the named modal rather than the hidden main. Long public text wraps within its own detail surface. Contact values render only after an explicit click and remain plain text.

Baseline capture is separate from the final comparison gate. Initial missing baselines and failed setup/assertion runs are not completion evidence. On final product/test source `c6c04472021ad80a8127697b1841b7f724f12877`, fresh Linux comparison passes all four suites: original visual regression 16 tests, R2 review 1, R1 navigation/logout 1, R3 recruitment 2. Every report has zero failed, skipped or flaky tests. These are comparisons against committed baselines, without `--update-snapshots`; no pixel or axe threshold changed. Full logs and the four v5 JSON reports are retained with the exact source-identity receipt in innovation-recruitment-verification.md.

Review guidance was read from the installed web-design-guidelines skill and [its current upstream checklist](https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md); repository semantics and the narrow authorized-contact exception take precedence.
