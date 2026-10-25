"use client";
/**
 * The innovation module's campus shell (batch 1 — plan
 * 2026-10-24-innovation-talent-merge-batch1 Task 9): the minimal
 * section shell for the (campus) route group.
 *
 * Browsing is open to every ACTIVE in-school identity (the backend's
 * `Viewer` guard is the authority); the shell only mirrors it — loading
 * and session-outage keep the workspace geometry, an anonymous visitor
 * gets the login pointer instead of a page of 401ing sections (design
 * §10), and a non-ACTIVE account gets the honest permission line.
 *
 * The rail is the shared WorkspaceSidebar (the same primitive Student/
 * Teacher/Admin shells ride) carrying the innovation navigation: 人才库
 * and 项目库 are live (batch 2), while 校内成果 and 正在招募 stay HONEST
 * placeholders — inert muted rows that say "(即将开放)", never
 * aria-disabled links (they become real items as batches 3–4 land, in
 * place, without moving).
 *
 * This file is the ONLY client boundary of the group (patterns §2);
 * pages inside stay Server Components that mount their own data
 * islands.
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { WorkspaceSidebar, navItemActive, type SideNavItem } from "@/components/shell/WorkspaceSidebar";
import { ReviewIcon, SlotsIcon, TasksIcon, TrophyIcon } from "@/components/shell/navIcons";
import { useSession } from "@/features/auth/session";
import { landingPathForRole } from "@/features/auth/workspace";
import { getAuthEpoch } from "@/lib/accessToken";

/* The innovation navigation contract: two live destinations (batch 2
 * opens 项目库 as a real route) plus the two not-yet-opened sections.
 * hrefs name where each section WILL live so later batches only flip
 * `disabled` off. */
const INNOVATION_NAV: readonly SideNavItem[] = [
  { href: "/innovation/talents", label: "人才库", icon: <SlotsIcon width="20" height="20" strokeWidth="1.8" /> },
  { href: "/innovation/projects", label: "项目库", icon: <TasksIcon /> },
  { href: "/innovation/achievements", label: "校内成果(即将开放)", icon: <TrophyIcon />, disabled: true },
  { href: "/innovation/recruitments", label: "正在招募(即将开放)", icon: <ReviewIcon />, disabled: true },
];

const SIDEBAR_GROUPS = [
  { label: "创新创业", items: INNOVATION_NAV },
] as const;

/** The medium/narrow top nav — the same items as the rail, so no band
 *  shows a different picture of the section (links live, placeholders
 *  stay inert muted text). */
function InnovationTopNav({ pathname }: { pathname: string }) {
  return (
    <nav className="app-nav" aria-label="创新创业导航">
      {INNOVATION_NAV.map((item) =>
        item.disabled ? (
          <span key={item.href} className="app-nav-item-disabled">
            {item.label}
          </span>
        ) : (
          <Link
            key={item.href}
            href={item.href}
            aria-current={navItemActive(pathname, item.href) ? "page" : undefined}
          >
            {item.label}
          </Link>
        ),
      )}
    </nav>
  );
}

export function InnovationShell({ children }: { children: ReactNode }) {
  const { state, refresh } = useSession();
  const pathname = usePathname();

  return (
    <div className="app-shell" data-shell="campus-innovation">
      <WorkspaceSidebar brand={{ href: "/", label: "CampusQuest" }} groups={SIDEBAR_GROUPS} />
      <div className="app-body">
        <header className="app-topbar">
          <div className="app-topbar-inner">
            <Link className="app-brand" href="/">
              CampusQuest
            </Link>
            <InnovationTopNav pathname={pathname} />
            <div className="app-topbar-actions">
              {/* The way back to the visitor's own workspace — the
               * campus rail deliberately carries only the innovation
               * sections, so every role gets one explicit exit. */}
              {state.status === "authenticated" ? (
                <Link className="section-link" href={landingPathForRole(state.me.role)}>
                  返回工作台
                </Link>
              ) : null}
            </div>
          </div>
        </header>
        <main className="app-main" aria-busy={state.status === "loading" ? "true" : undefined}>
          {state.status === "loading" ? (
            <div role="status" aria-label="正在读取校内身份">
              <SectionSkeleton />
            </div>
          ) : state.status === "error" ? (
            <SectionError error={state.error} onRetry={refresh} />
          ) : state.status === "anonymous" ? (
            <p className="alert" role="alert">
              请先
              <Link href="/login">登录校内账号</Link>
              后浏览双创内容。
            </p>
          ) : state.me.status !== "ACTIVE" ? (
            <p className="alert alert-warning" role="alert">
              当前账号状态不允许浏览校内双创内容。
            </p>
          ) : (
            <div key={`${state.me.id}:${getAuthEpoch()}`}>{children}</div>
          )}
        </main>
      </div>
    </div>
  );
}
