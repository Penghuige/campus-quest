import type { ReactNode } from "react";

import { InnovationShell } from "@/features/innovation/InnovationShell";

/**
 * Server frame for the campus (innovation) route group (patterns §2):
 * the shell is one client island (session gate + the innovation
 * navigation); every page inside stays a Server Component that mounts
 * its own data islands.
 */
export default function CampusLayout({ children }: { children: ReactNode }) {
  return <InnovationShell>{children}</InnovationShell>;
}
