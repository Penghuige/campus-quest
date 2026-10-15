"use client";
/**
 * The staff shells' loading frame (owner feedback 2026-10-15, third
 * item): PATH IS GEOMETRY — an /admin/* or /teacher/* document renders
 * its workspace's own chrome shape from the very first frame (the
 * route group itself is the geometry hint; no cookie needed), so a
 * refresh never paints the white-bar topbar the old loading branch
 * showed. The student shell's optimistic-workspace philosophy (I15)
 * generalized to the staff groups: sidebar rail + topbar nav are
 * static config and render for real; only the USER-data slots (the
 * account cluster) are placeholder skeletons, mirroring the student
 * OptimisticWorkspaceShell's aria-hidden action cluster.
 *
 * Non-admin/teacher sessions see this frame too during resolve — the
 * gate then renders its guidance card INSIDE the same shell family
 * (minimal), an accepted within-family correction, never a cross-shape
 * flash of a DIFFERENT workspace style.
 */
import Link from "next/link";
import type { ReactNode } from "react";

import { WorkspaceSidebar, navItemActive, type SideNavItem } from "./WorkspaceSidebar";
import { usePathname } from "next/navigation";

export function StaffShellSkeleton({
  brandHref,
  navLabel,
  navItems,
  sidebarGroupLabel,
  children,
}: {
  brandHref: string;
  navLabel: string;
  navItems: readonly SideNavItem[];
  sidebarGroupLabel: string;
  /** The page content's own loading placeholder. */
  children?: ReactNode;
}) {
  const pathname = usePathname();
  const groups = [{ label: sidebarGroupLabel, items: navItems }] as const;

  return (
    <div className="app-shell" data-shell="loading">
      <WorkspaceSidebar brand={{ href: brandHref, label: "CampusQuest" }} groups={groups} />
      <div className="app-body">
        <header className="app-topbar">
          <div className="app-topbar-inner">
            <span className="app-brand">CampusQuest</span>
            <nav className="app-nav" aria-label={navLabel}>
              {navItems.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={navItemActive(pathname, item.href) ? "page" : undefined}
                >
                  {item.label}
                </Link>
              ))}
            </nav>
            <div className="app-topbar-actions" aria-hidden="true">
              <span className="avatar-chip skeleton" />
            </div>
          </div>
        </header>
        <main className="app-main" aria-busy="true">
          {children ?? (
            <>
              <span className="skeleton skeleton-line" style={{ width: "40%" }} />
              <span className="skeleton skeleton-line" />
              <span className="skeleton skeleton-block" />
            </>
          )}
        </main>
      </div>
    </div>
  );
}
