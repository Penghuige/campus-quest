import type { ReactNode } from "react";

import { StudentShell } from "./_components/StudentShell";

/**
 * Server frame for the student route group (patterns §2): the shell is one
 * client island (session gate + navigation); every page inside stays a
 * Server Component that renders its own client data islands.
 */
export default function StudentLayout({ children }: { children: ReactNode }) {
  return <StudentShell>{children}</StudentShell>;
}
