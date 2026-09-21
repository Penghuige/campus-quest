"use client";
/**
 * Authenticated student shell (patterns §2/§8): client island wrapping the
 * whole (student) route group. The session hook (SWR-style /me cache)
 * gates the group — anonymous visitors get the login CTA instead of a
 * page of failing sections, a session outage gets a retryable error, and
 * only authenticated users reach the pages (design §10).
 *
 * Pages inside stay Server Components; this file is the ONLY client
 * boundary of the shell (patterns §2: isolate the interactive child).
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { SectionError } from "@/components/ui/sectionStates";
import { useSession } from "@/features/auth/session";

const NAV_ITEMS = [
  { href: "/", label: "首页" },
  { href: "/tasks", label: "任务" },
  { href: "/rankings", label: "排行榜" },
  { href: "/rewards", label: "奖励" },
  { href: "/profile", label: "我的" },
  // My Claims / Notifications land with their own S4 tasks — append
  // here as the routes appear (design §8).
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

  return (
    <div className="app-shell">
      <header className="app-topbar">
        <div className="app-topbar-inner">
          <span className="app-brand">CampusQuest</span>
          <nav className="app-nav" aria-label="主导航">
            {NAV_ITEMS.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                aria-current={pathname === item.href ? "page" : undefined}
              >
                {item.label}
              </Link>
            ))}
          </nav>
          <span className="app-user" title={state.me.nickname}>
            {state.me.nickname}
          </span>
        </div>
      </header>
      <main className="app-main">{children}</main>
    </div>
  );
}
