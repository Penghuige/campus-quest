import { ReviewIcon } from "@/components/shell/navIcons";
import type { SideNavItem } from "@/components/shell/WorkspaceSidebar";

/**
 * Public browsing and the independently authorized operations workspace.
 *
 * Batch 2 keeps the working tree's SideNavItem contract (no PR-only
 * `exclude` field); the current Student/Teacher/Admin shells still
 * build their innovation entries locally, so this module is the shared
 * contract for the batch-4 shell integration, not a mounted nav today.
 */
export const INNOVATION_NAV: SideNavItem = {
  href: "/innovation",
  label: "创新创业",
  icon: <ReviewIcon />,
};

export const OPERATIONS_NAV: SideNavItem = {
  href: "/innovation/reviews",
  label: "双创运营工作台",
  icon: <ReviewIcon />,
};
