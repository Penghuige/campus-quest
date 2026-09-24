"use client";
/**
 * Plan 11 Task 3 — the shared desktop sidebar primitive (brief §4.1,
 * frozen navigation contract). One component serves Student, Teacher,
 * and Admin: role shells pass their own items; geometry is shared.
 *
 * Contract rules implemented here:
 * - the sidebar is visually quieter than the content (surface step
 *   down, muted links);
 * - exactly ONE item per nav landmark carries `aria-current="page"`
 *   ("/" matches exactly so the dashboard is never active elsewhere);
 * - items are real links in standard tab order (no JS activation).
 *
 * Hidden below 64rem by CSS (medium band keeps the horizontal top
 * nav; narrow band uses the student bottom nav or staff menu sheet).
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

export interface SideNavItem {
  href: string;
  label: string;
  icon?: ReactNode;
}

export interface SideNavGroup {
  /** Landmark name for assistive tech; also groups the links visually. */
  label: string;
  items: readonly SideNavItem[];
}

/** Shared prefix-active rule ("/" is exact so it cannot win subroutes). */
export function navItemActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

export function WorkspaceSidebar({
  brand,
  groups,
  footer,
}: {
  brand: { href: string; label: string };
  groups: readonly SideNavGroup[];
  footer?: ReactNode;
}) {
  const pathname = usePathname();
  return (
    <aside className="app-sidebar">
      <Link className="app-sidebar-brand" href={brand.href}>
        {brand.label}
      </Link>
      {groups.map((group) => (
        <nav key={group.label} className="side-nav" aria-label={group.label}>
          {group.items.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              aria-current={navItemActive(pathname, item.href) ? "page" : undefined}
            >
              {item.icon}
              <span>{item.label}</span>
            </Link>
          ))}
        </nav>
      ))}
      {footer !== undefined && <div className="app-sidebar-footer">{footer}</div>}
    </aside>
  );
}
