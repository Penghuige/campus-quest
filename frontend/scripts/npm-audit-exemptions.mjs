/**
 * npm audit exemptions (P1 visibility): advisories the gate deliberately
 * does not fail on. Every entry MUST carry the reason and a re-review
 * date; an entry past its date fails `check-audit` until re-triaged.
 *
 * Gate semantics: only HIGH and CRITICAL gate (the CI step's contract);
 * moderate/low are reported but never fail. An exemption removes a
 * module from the failing set entirely (all its advisories) — keep
 * entries module-granular so re-triage is precise.
 */
export const AUDIT_EXEMPTIONS = [
  {
    module: "braces",
    severity: "high",
    reason:
      "Dev-toolchain transitive (via eslint-config-next 16.x -> " +
      "@next/eslint-plugin-next -> fast-glob -> micromatch -> braces). " +
      "Lint tooling never ships in the artifact; upstream's only " +
      "offered 'fix' is a semver-major DOWNGRADE to eslint-config-next " +
      "14, which is wrong for a Next 16 app. Re-exempt per advisory as " +
      "long as 16.x carries the chain.",
    reviewBy: "2027-01-07",
  },
  {
    module: "micromatch",
    severity: "high",
    reason: "Same eslint-config-next dev chain as braces (see above).",
    reviewBy: "2027-01-07",
  },
  {
    module: "fast-glob",
    severity: "high",
    reason: "Same eslint-config-next dev chain as braces (see above).",
    reviewBy: "2027-01-07",
  },
  {
    module: "@next/eslint-plugin-next",
    severity: "high",
    reason:
      "The head of the exempted dev chain; the advisory reach is the " +
      "braces/micromatch glob parsing below it. Not in the shipped build.",
    reviewBy: "2027-01-07",
  },
  {
    module: "eslint-config-next",
    severity: "high",
    reason:
      "Aggregate advisory node for the dev-chain entries above; pinned " +
      "to the current 16.x line. Re-triage when eslint-config-next 16.x " +
      "bumps its glob dependencies.",
    reviewBy: "2027-01-07",
  },
];
