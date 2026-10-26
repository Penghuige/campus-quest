import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import { StudentShell } from "./_components/StudentShell";
import { CSRF_COOKIE_NAME } from "@/lib/csrf";
import {
  shellHintFromCookieValue,
  shellRedirectsToLogin,
  SHELL_HINT_COOKIE_NAME,
} from "@/features/auth/shellChrome";

/**
 * Server frame for the student route group (patterns §2): the shell is one
 * client island (session gate + navigation); every page inside stays a
 * Server Component that renders its own client data islands.
 *
 * Session-evidence probe (backlog UX: the refresh shell jump): the
 * readable `csrf_token` cookie is the only session artifact a page
 * request ever carries — the refresh cookie is HttpOnly AND
 * path-scoped to the auth endpoints, so it never rides a document
 * request. Its PRESENCE (never the value) tells the shell whether the
 * optimistic workspace chrome may render during the session-resolve
 * window, so a refresh paints the authenticated geometry in the very
 * first server HTML. Reading it opts the (student) subtree into
 * dynamic rendering — these pages were never cacheable payloads
 * anyway (every datum is a per-user client fetch).
 *
 * Role-hint probe (the staff wrong-shell flash fix, plan A): csrf
 * evidence is role-blind — a STAFF session refreshing this group
 * SSR'd the student workspace and repainted to the guidance card. The
 * mint's cq_shell hint cookie (HttpOnly; this SERVER-side read is its
 * only consumer) routes staff sessions to the minimal pre-resolve
 * chrome, their terminal geometry here. Absence keeps the legacy
 * behavior (see shellChrome.ts's three-write-point lifecycle).
 */
export default async function StudentLayout({ children }: { children: ReactNode }) {
  const cookieStore = await cookies();
  const shellHint = shellHintFromCookieValue(
    cookieStore.get(SHELL_HINT_COOKIE_NAME)?.value,
  );
  const sessionCookiePresent = cookieStore.has(CSRF_COOKIE_NAME);
  // The no-evidence direct path (owner feedback 2026-10-26): a
  // visitor with NEITHER session evidence NOR a role hint is a
  // first-time user — hand them to the login page on the SERVER, so
  // the login card is their first paint and the one-frame workspace
  // skeleton the client would immediately 401 away never renders.
  if (shellRedirectsToLogin(sessionCookiePresent, shellHint)) {
    redirect("/login");
  }
  return (
    <StudentShell sessionCookiePresent={sessionCookiePresent} shellHint={shellHint}>
      {children}
    </StudentShell>
  );
}
