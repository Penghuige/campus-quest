"use client";
/**
 * Plan 11 Task 3 — the Student narrow bottom navigation (frozen
 * contract: exactly 5 slots, fixed list). A `<nav>` landmark of plain
 * links (keyboard-operable, no JS activation); CSS pins it to the
 * bottom with the safe-area inset and reserves matching content
 * padding so it can never cover a primary action.
 *
 * Rendered by the student shell; hidden at/above 40rem by CSS.
 */
import Link from "next/link";
import { usePathname } from "next/navigation";

import { navItemActive, type SideNavItem } from "./WorkspaceSidebar";

export function BottomNav({ items }: { items: readonly SideNavItem[] }) {
  const pathname = usePathname();
  return (
    <nav className="app-bottomnav" aria-label="主导航">
      {items.map((item) => (
        <Link
          key={item.href}
          href={item.href}
          aria-current={navItemActive(pathname, item.href) ? "page" : undefined}
        >
          {item.icon}
          <span className="app-bottomnav-label">{item.label}</span>
        </Link>
      ))}
    </nav>
  );
}
