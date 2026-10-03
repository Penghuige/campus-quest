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
 *
 * Defect #3 (QA 2026-09-30) adds two GEOMETRY-only affordances on top
 * of that contract — the nav items themselves stay real links:
 * - a collapse toggle (brand row) that swaps the rail to an icon rail;
 * - a right-edge resizer: pointer drag, or the keyboard equivalent
 *   (role="separator" with Arrow/Home/End stepping, the WAI-ARIA
 *   window-splitter pattern) for the same pixel range.
 * The preference (collapsed flag + expanded width) is non-secret UI
 * state in localStorage (lib/sidebarPreference.ts), applied AFTER
 * mount so SSR markup never depends on it; there is no width
 * transition either way (motion contract: transform/opacity only).
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";

import { BrandMark } from "./BrandMark";
import { PanelCollapseIcon, PanelExpandIcon } from "./navIcons";
import {
  clampRailWidth,
  RAIL_WIDTH_DEFAULT_PX,
  RAIL_WIDTH_MAX_PX,
  RAIL_WIDTH_MIN_PX,
  readSidebarPreference,
  writeSidebarPreference,
  type SidebarPreference,
} from "@/lib/sidebarPreference";

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

/** Keyboard step for the resizer (a deliberate nudge, not a pixel). */
const RESIZE_KEY_STEP_PX = 16;

const DEFAULT_PREFERENCE: SidebarPreference = {
  collapsed: false,
  widthPx: RAIL_WIDTH_DEFAULT_PX,
};

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
  // SSR and the first client render agree on the defaults; the stored
  // preference is applied in the mount effect (post-hydration), and
  // hydration itself never reads localStorage.
  const [pref, setPref] = useState<SidebarPreference>(DEFAULT_PREFERENCE);
  const dragStart = useRef<{
    pointerId: number;
    startX: number;
    startWidth: number;
  } | null>(null);

  // Load once after mount; a missing/corrupt entry keeps the defaults.
  // Deferred a microtask so the effect body stays free of synchronous
  // setState (react-hooks/set-state-in-effect) — the session.ts
  // precedent; the cancelled flag fences an unmount inside the gap.
  useEffect(() => {
    const stored = readSidebarPreference(window.localStorage);
    if (stored === null) return;
    let cancelled = false;
    Promise.resolve().then(() => {
      if (!cancelled) {
        setPref(stored);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // Mirror the geometry into the --rail-width token the shell grid
  // consumes. Collapsed resolves THROUGH the collapsed token so the
  // icon-rail width stays rem-based (it scales with the user's font
  // size even though the drag itself works in screen pixels).
  useEffect(() => {
    document.documentElement.style.setProperty(
      "--rail-width",
      pref.collapsed ? "var(--rail-width-collapsed)" : `${pref.widthPx}px`,
    );
  }, [pref]);

  /** Every user-initiated change: state + explicit persist (no write
   * effect — mount/default reloads must not create a preference). */
  const updatePref = useCallback((next: SidebarPreference) => {
    setPref(next);
    writeSidebarPreference(window.localStorage, next);
  }, []);

  const toggleCollapsed = useCallback(() => {
    updatePref({ ...pref, collapsed: !pref.collapsed });
  }, [pref, updatePref]);

  // --- resizer: pointer drag (capture keeps events on the handle) ---
  const onResizePointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.pointerType === "mouse" && event.button !== 0) return;
      dragStart.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startWidth: pref.widthPx,
      };
      event.currentTarget.setPointerCapture(event.pointerId);
    },
    [pref.widthPx],
  );

  const onResizePointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const drag = dragStart.current;
      if (drag === null || drag.pointerId !== event.pointerId) return;
      const next = clampRailWidth(drag.startWidth + event.clientX - drag.startX);
      if (next !== pref.widthPx) {
        setPref((current) => ({ ...current, widthPx: next }));
      }
    },
    [pref.widthPx],
  );

  const endResizeDrag = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const drag = dragStart.current;
      if (drag === null || drag.pointerId !== event.pointerId) return;
      dragStart.current = null;
      // Persist the settled width once, at drag end (not per move).
      writeSidebarPreference(window.localStorage, pref);
    },
    [pref],
  );

  // --- resizer: keyboard equivalent (WAI-ARIA window splitter) ---
  const onResizeKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      let next: number;
      switch (event.key) {
        case "ArrowLeft":
        case "ArrowDown":
          next = pref.widthPx - RESIZE_KEY_STEP_PX;
          break;
        case "ArrowRight":
        case "ArrowUp":
          next = pref.widthPx + RESIZE_KEY_STEP_PX;
          break;
        case "Home":
          next = RAIL_WIDTH_MIN_PX;
          break;
        case "End":
          next = RAIL_WIDTH_MAX_PX;
          break;
        default:
          return;
      }
      event.preventDefault();
      updatePref({ ...pref, widthPx: clampRailWidth(next) });
    },
    [pref, updatePref],
  );

  return (
    <aside
      className="app-sidebar"
      id="app-sidebar-rail"
      data-collapsed={pref.collapsed ? "true" : "false"}
    >
      <div className="app-sidebar-brandbar">
        <Link
          className="app-sidebar-brand"
          href={brand.href}
          title={pref.collapsed ? brand.label : undefined}
        >
          <BrandMark />
          <span className="app-sidebar-brand-name">{brand.label}</span>
        </Link>
        <button
          type="button"
          className="rail-toggle"
          onClick={toggleCollapsed}
          aria-expanded={!pref.collapsed}
          aria-controls="app-sidebar-rail"
          aria-label={pref.collapsed ? "展开侧边栏" : "收起侧边栏"}
          title={pref.collapsed ? "展开侧边栏" : "收起侧边栏"}
        >
          {pref.collapsed ? <PanelExpandIcon /> : <PanelCollapseIcon />}
        </button>
      </div>
      {groups.map((group) => (
        <nav key={group.label} className="side-nav" aria-label={group.label}>
          {group.items.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              aria-current={navItemActive(pathname, item.href) ? "page" : undefined}
              title={pref.collapsed ? item.label : undefined}
            >
              {item.icon}
              <span>{item.label}</span>
            </Link>
          ))}
        </nav>
      ))}
      {footer !== undefined && <div className="app-sidebar-footer">{footer}</div>}
      <div
        className="rail-resizer"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整侧边栏宽度"
        title="拖动或用左右方向键调整宽度"
        tabIndex={0}
        aria-valuemin={RAIL_WIDTH_MIN_PX}
        aria-valuemax={RAIL_WIDTH_MAX_PX}
        aria-valuenow={pref.widthPx}
        onPointerDown={onResizePointerDown}
        onPointerMove={onResizePointerMove}
        onPointerUp={endResizeDrag}
        onPointerCancel={endResizeDrag}
        onKeyDown={onResizeKeyDown}
      />
    </aside>
  );
}
