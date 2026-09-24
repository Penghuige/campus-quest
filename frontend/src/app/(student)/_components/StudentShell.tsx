"use client";
/**
 * Authenticated student shell (patterns §2/§8): client island wrapping the
 * whole (student) route group. The session hook (SWR-style /me cache)
 * gates the group — anonymous visitors get the login CTA instead of a
 * page of failing sections, a session outage gets a retryable error, and
 * only STUDENT sessions reach the pages (design §10); a TEACHER/ADMIN
 * session gets guidance to the staff workspace (see `workspace.ts`).
 *
 * Pages inside stay Server Components; this file is the ONLY client
 * boundary of the shell (patterns §2: isolate the interactive child).
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { SectionError } from "@/components/ui/sectionStates";
import { BottomNav } from "@/components/shell/BottomNav";
import { WorkspaceSidebar, navItemActive } from "@/components/shell/WorkspaceSidebar";
import { GiftIcon, HomeIcon, InboxIcon, TasksIcon, TrophyIcon, UserIcon } from "@/components/shell/navIcons";
import { useSession } from "@/features/auth/session";
import { studentWorkspaceGate } from "@/features/auth/workspace";
import { NotificationBell } from "@/features/notifications/NotificationBell";

/* Plan 11 Task 3 — the frozen navigation contract (route-true
 * amendment): desktop sidebar primary + secondary, narrow bottom nav.
 * 我的任务 stays anchored on the dashboard (its only list surface);
 * 社区 stays anchored on task detail — V1 has neither route. */
const SIDEBAR_PRIMARY = [
  { href: "/", label: "首页", icon: <HomeIcon /> },
  { href: "/tasks", label: "任务", icon: <TasksIcon /> },
  { href: "/rankings", label: "排行榜", icon: <TrophyIcon /> },
  { href: "/rewards", label: "积分奖励", icon: <GiftIcon /> },
] as const;

const SIDEBAR_SECONDARY = [
  { href: "/notifications", label: "通知", icon: <InboxIcon /> },
  { href: "/profile", label: "我的", icon: <UserIcon /> },
] as const;

const SIDEBAR_GROUPS = [
  { label: "主导航", items: SIDEBAR_PRIMARY },
  { label: "个人", items: SIDEBAR_SECONDARY },
] as const;

/* Medium band (40–64rem): the horizontal top nav keeps every
 * destination — no reachability is lost between sidebar and bottom. */
const MEDIUM_NAV = [
  { href: "/", label: "首页" },
  { href: "/tasks", label: "任务" },
  { href: "/rankings", label: "排行榜" },
  { href: "/rewards", label: "奖励" },
  { href: "/notifications", label: "通知" },
  { href: "/profile", label: "我的" },
] as const;

const BOTTOM_NAV = [
  { href: "/", label: "首页", icon: <HomeIcon /> },
  { href: "/tasks", label: "任务", icon: <TasksIcon /> },
  { href: "/rankings", label: "排行榜", icon: <TrophyIcon /> },
  { href: "/rewards", label: "积分奖励", icon: <GiftIcon /> },
  { href: "/profile", label: "我的", icon: <UserIcon /> },
] as const;

export function StudentShell({ children }: { children: ReactNode }) {
  const { state, refresh } = useSession();
  const pathname = usePathname();

  if (state.status === "loading") {
    return (
      <div className="app-shell">
        <div className="app-topbar">
          <div className="app-topbar-inner">
            <span className="app-brand">CampusQuest</span>
          </div>
        </div>
        <main className="app-main" aria-busy="true">
          <span className="skeleton skeleton-line" style={{ width: "40%" }} />
          <span className="skeleton skeleton-line" />
          <span className="skeleton skeleton-block" />
        </main>
      </div>
    );
  }

  if (state.status === "anonymous") {
    return (
      <div className="app-shell">
        <main className="auth-shell">
          <div className="auth-card">
            <div className="auth-head">
              <h1 className="auth-title">登录 CampusQuest</h1>
              <p className="auth-subtitle">
                领取任务单元、累积积分并兑换奖励
              </p>
            </div>
            <Link className="btn btn-primary btn-block" href="/login">
              去登录
            </Link>
            <p className="auth-alt-action">
              还没有账号？
              <Link className="link" href="/register">
                注册学生账号
              </Link>
            </p>
          </div>
        </main>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className="app-shell">
        <div className="app-topbar">
          <div className="app-topbar-inner">
            <span className="app-brand">CampusQuest</span>
          </div>
        </div>
        <main className="app-main">
          <SectionError error={state.error} onRetry={refresh} retryLabel="重新加载" />
        </main>
      </div>
    );
  }

  // Role gate (PR #4 hardening Task 3): the student workspace mounts
  // ONLY for a STUDENT session. A TEACHER/ADMIN session gets guidance
  // to the staff workspace instead — and because the pages (children)
  // never mount, none of the student-only sections can fire their API
  // calls from a staff session.
  const gate = studentWorkspaceGate(state.me.role);
  if (gate.kind !== "student") {
    return (
      <div className="app-shell">
        <div className="app-topbar">
          <div className="app-topbar-inner">
            <span className="app-brand">CampusQuest</span>
          </div>
        </div>
        <main className="app-main">
          <div className="alert alert-warning" role="alert">
            <p>
              <span className="alert-marker" aria-hidden="true">!</span>
              学生工作区仅对学生账号开放，当前登录的是教师或管理员账号。
            </p>
            <p>
              <Link className="btn btn-primary" href={gate.workspacePath}>
                前往教师工作台
              </Link>
            </p>
          </div>
        </main>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <WorkspaceSidebar
        brand={{ href: "/", label: "CampusQuest" }}
        groups={SIDEBAR_GROUPS}
      />
      <div className="app-body">
        <header className="app-topbar">
          <div className="app-topbar-inner">
            <span className="app-brand">CampusQuest</span>
            <nav className="app-nav" aria-label="主导航">
              {MEDIUM_NAV.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={navItemActive(pathname, item.href) ? "page" : undefined}
                >
                  {item.label}
                </Link>
              ))}
            </nav>
            <div className="app-topbar-actions">
              <NotificationBell />
              <span className="app-user" title={state.me.nickname}>
                {state.me.nickname}
              </span>
            </div>
          </div>
        </header>
        <main className="app-main">{children}</main>
      </div>
      <BottomNav items={BOTTOM_NAV} />
    </div>
  );
}
