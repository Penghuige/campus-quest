import type { ReactNode } from "react";

import { TeacherShell } from "./_components/TeacherShell";

/**
 * Server frame for the teacher workspace (patterns §2): the staff shell
 * is one client island (session gate + staff navigation); every page
 * inside stays a Server Component that renders its own client data
 * islands. The guard reuses the shared session (no second /me surface).
 */
export default function TeacherLayout({ children }: { children: ReactNode }) {
  return <TeacherShell>{children}</TeacherShell>;
}
