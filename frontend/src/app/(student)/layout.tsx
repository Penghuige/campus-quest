import { cookies } from "next/headers";
import type { ReactNode } from "react";

import { StudentShell } from "./_components/StudentShell";
import { CSRF_COOKIE_NAME } from "@/lib/csrf";

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
 */
export default async function StudentLayout({ children }: { children: ReactNode }) {
  const cookieStore = await cookies();
  return (
    <StudentShell sessionCookiePresent={cookieStore.has(CSRF_COOKIE_NAME)}>
      {children}
    </StudentShell>
  );
}
