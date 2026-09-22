import type { ReactNode } from "react";

import { AdminShell } from "./_components/AdminShell";

/**
 * Server frame for the admin workspace (patterns §2; the teacher-layout
 * precedent): the admin shell is one client island (session gate + admin
 * navigation); every page inside stays a Server Component rendering its
 * own client data islands. The ADMIN-only gate means a non-admin session
 * never mounts the pages, so no admin API request fires at all.
 */
export default function AdminLayout({ children }: { children: ReactNode }) {
  return <AdminShell>{children}</AdminShell>;
}
