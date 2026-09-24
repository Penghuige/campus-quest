"use client";
/**
 * Plan 11 Task 3 — the staff narrow menu sheet (frozen contract: the
 * FULL staff navigation list, nothing demoted beyond opening it).
 *
 * A native `<dialog>`: the platform's focus trap and Escape handling
 * satisfy the contract's keyboard rules; clicking the backdrop (the
 * dialog element itself) closes it, and any navigation closes it so
 * the sheet never strands the user on a stale page.
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";

import { MenuIcon } from "./navIcons";
import { navItemActive, type SideNavItem } from "./WorkspaceSidebar";

export function StaffMenuSheet({
  label,
  items,
}: {
  label: string;
  items: readonly SideNavItem[];
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const pathname = usePathname();

  // Any navigation closes the sheet (a chosen destination or a
  // back-navigation — the menu must not outlive its page).
  useEffect(() => {
    dialogRef.current?.close();
  }, [pathname]);

  return (
    <>
      <button
        type="button"
        className="app-menubtn"
        aria-haspopup="dialog"
        onClick={() => dialogRef.current?.showModal()}
      >
        <MenuIcon />
        <span className="app-menubtn-label">菜单</span>
      </button>
      <dialog
        ref={dialogRef}
        className="dialog app-menusheet"
        aria-label={label}
        onClick={(event) => {
          if (event.target === dialogRef.current) {
            dialogRef.current.close();
          }
        }}
      >
        <div className="dialog-body">
          <nav className="app-menusheet-nav" aria-label={label}>
            {items.map((item) => (
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
        </div>
      </dialog>
    </>
  );
}
