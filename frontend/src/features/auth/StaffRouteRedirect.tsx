"use client";
/**
 * The staff-on-student-route redirect island (audit item A; owner
 * feedback 2026-10-15): a TEACHER/ADMIN session opening a student
 * route lands on ITS OWN workspace — the guidance card is retired for
 * this family (misentry and stale bookmarks both deserve the jump;
 * true cross-role errors keep their cards elsewhere).
 *
 * Renders the same quiet placeholder the pre-resolve frames carry
 * (正在前往工作台… — minimal geometry on BOTH sides of the jump, so
 * the redirect is a URL change, never a shape change) and performs the
 * replace ONCE on mount: the role comes from the resolved session,
 * never the cookie hint (evidence selects the PLACEHOLDER's geometry;
 * /me's answer selects the DESTINATION).
 */
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { landingPathForRole } from "./workspace";
import type { components } from "@/lib/api/schema";

type Role = components["schemas"]["Role"];

export function StaffRouteRedirect({ role }: { role: Role }) {
  const router = useRouter();

  useEffect(() => {
    router.replace(landingPathForRole(role));
    router.refresh();
  }, [router, role]);

  return (
    <p className="field-hint" role="status">
      正在前往工作台…
      <span className="spinner" aria-hidden="true" />
    </p>
  );
}
