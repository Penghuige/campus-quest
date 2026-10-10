/**
 * P3-A extraction: the 11-shot surface collection shared by the pixel
 * suite and the axe accessibility suite — ONE source of truth for
 * which pages the visual/a11y gates cover. This module is deliberately
 * SIDE-EFFECT-FREE (no test.skip/test.use at module level — those
 * poison any importer's registry, the lesson from the first axe run);
 * the per-shot env guards inside authenticate run within a test.
 */
import { type Locator, type Page } from "@playwright/test";

import { ensureStudentLogin, staffLogin, test } from "./fixtures";

export type { Locator, Page };

interface ShotMask {
  /** Stable id used in assertion messages. */
  id: string;
  /** Why this region is volatile (what clock/id fact it hides). */
  why: string;
  locate: (page: Page) => Locator;
}

export interface Shot {
  name: string;
  auth: ShotAuth;
  masks: ShotMask[];
  /** Static path (matrix routes)… */
  path?: string;
  /** …or a world-export env carrying a seeded deep link (claim/task
   * detail): the shot skips when the export is absent, mirroring
   * visual-capture.spec.ts's conditional-capture discipline. These two
   * shots are deliberately NOT in scripts/assert-e2e-no-skips.mjs's
   * watched list — the skip is legal. */
  envPath?: string;
}

export const SHOTS: Shot[] = [
  { name: "auth-login", path: "/login", auth: "anon", masks: [] },
  { name: "dev-gallery", path: "/dev/gallery", auth: "anon", masks: [] },
  {
    name: "student-dashboard",
    path: "/",
    auth: "student",
    masks: [
      {
        id: ".claim-deadline",
        why: "claim-row 截止…· 还剩… — relative-clock text (DashboardView.tsx)",
        locate: (page) => page.locator(".claim-deadline"),
      },
      {
        id: ".hero-line~截止",
        why: "hero deadline — the same relative-clock copy; .hero-line is shared with the deterministic step rail, so the mask keys on the frozen 截止 copy (DashboardView.tsx)",
        locate: (page) => page.locator(".hero-line", { hasText: "截止" }),
      },
      {
        id: ".task-grid .meta-deadline",
        why: "discovery-card deadline line — the square's cards carry the mint-relative countdown (TaskCard.tsx); same clock family the claim/hero masks cover (2026-10-10 baseline time-drift ruling)",
        locate: (page) => page.locator(".task-grid .meta-deadline"),
      },
      // Deliberately NOT masked: the reward-progress .progress-note
      // ("（50 积分）现在就可以兑换") is deterministic under fixed labels.
    ],
  },
  {
    name: "student-tasks",
    path: "/tasks",
    auth: "student",
    masks: [
      {
        id: ".task-grid .meta-deadline",
        why: "card deadline line — mint-relative countdown (TaskCard.tsx); the same mechanism the dashboard mask covers (2026-10-10 baseline time-drift ruling)",
        locate: (page) => page.locator(".task-grid .meta-deadline"),
      },
    ],
  },
  {
    name: "student-task-detail",
    envPath: "CQ_E2E_TASK_OPEN_PATH",
    auth: "student",
    // Probe proof (follow-up run): the page's only candidate, its
    // .deadline-line, renders "领取后 4320 分钟内提交" — derived from the
    // seeded task's fixed RELATIVE duration, deterministic across runs.
    masks: [],
  },
  {
    name: "student-claim",
    envPath: "CQ_E2E_CLAIM_PATH",
    auth: "student",
    masks: [
      {
        id: ".assignment-item~领取时间 .assignment-value",
        why: "领取时间 = claimed_at — absolute clock (ClaimDetailView.tsx)",
        locate: (page) =>
          page
            .locator(".assignment-item", { hasText: "领取时间" })
            .locator(".assignment-value"),
      },
      {
        id: ".deadline-line",
        why: "截止…· 还剩… — relative-clock countdown (ClaimDetailView.tsx)",
        locate: (page) => page.locator(".deadline-line"),
      },
      {
        id: ".progress-note",
        why: "grace note 超过截止时间后至 <grace> 仍可提交 — absolute clock (ClaimDetailView.tsx)",
        locate: (page) => page.locator(".progress-note"),
      },
    ],
  },
  { name: "student-rankings", path: "/rankings", auth: "student", masks: [] },
  {
    name: "student-rewards",
    path: "/rewards",
    auth: "student",
    masks: [
      {
        id: ".reward-window",
        why: "redemption-window line — formatted absolute bounds, the shelf's one clock-derived string (RewardsView.tsx); the whole-row drift this mask kills was the student-rewards baseline archaeology's mechanism (2026-10-10 ruling)",
        locate: (page) => page.locator(".reward-window"),
      },
    ],
  },
  {
    name: "student-notifications",
    path: "/notifications",
    auth: "student",
    // Plan-13 T4: masks deliberately EMPTY — the thin world seeds zero
    // notifications, so the deterministic empty state is the captured
    // truth and no clock-volatile element exists to mask (see the header
    // probe note; the engagement contract makes an inert .notif-time
    // mask fail loud, so none is declared).
    masks: [],
  },
  {
    name: "teacher-reviews",
    path: "/teacher/reviews",
    auth: "teacher",
    masks: [
      {
        id: "time",
        why: "queue-row submitted_at — absolute clock (SubmissionReview.tsx)",
        locate: (page) => page.locator("time"),
      },
    ],
  },
  {
    name: "admin-users",
    path: "/admin/users",
    auth: "admin",
    masks: [
      {
        id: "time",
        why: "row created_at — absolute clock (AdminUserAccounts.tsx)",
        locate: (page) => page.locator("time"),
      },
      {
        id: "span.mono[title]",
        why: "run-unique user UUIDs (the username .mono cell carries no title attr, so this selects only the id cells)",
        locate: (page) => page.locator("span.mono[title]"),
      },
    ],
  },
];

export async function authenticate(page: Page, auth: ShotAuth): Promise<void> {
  switch (auth) {
    case "anon":
      return;
    case "student":
      return ensureStudentLogin(page);
    case "teacher": {
      test.skip(
        !(process.env.CQ_E2E_STAFF && process.env.CQ_E2E_STAFF_TOTP_SECRET),
        "world did not export the staff contract",
      );
      return staffLogin(
        page,
        process.env.CQ_E2E_STAFF!,
        process.env.CQ_E2E_STAFF_TOTP_SECRET!,
      );
    }
    case "admin": {
      test.skip(
        !(process.env.CQ_E2E_ADMIN && process.env.CQ_E2E_ADMIN_TOTP_SECRET),
        "world did not export the admin contract",
      );
      return staffLogin(
        page,
        process.env.CQ_E2E_ADMIN!,
        process.env.CQ_E2E_ADMIN_TOTP_SECRET!,
      );
    }
  }
}

