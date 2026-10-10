import { ReviewIcon } from "@/components/shell/navIcons";
import type { SideNavItem } from "@/components/shell/WorkspaceSidebar";

/** Public browsing and the independently authorized operations workspace. */
export const INNOVATION_NAV: SideNavItem = {
  href: "/innovation",
  label: "创新创业",
  icon: <ReviewIcon />,
  exclude: ["/innovation/reviews"],
};

export const OPERATIONS_NAV: SideNavItem = {
  href: "/innovation/reviews",
  label: "双创运营工作台",
  icon: <ReviewIcon />,
};
