"use client";
/**
 * Staff shell for the /admin workspace (design §8 Admin; patterns §2 —
 * the TeacherShell precedent).
 *
 * The session gate reuses the shared `useSession` cache with an
 * ADMIN-only rule (`adminWorkspaceGate`): `kind: "admin"` is the sole
 * branch that mounts the six admin pages, so a TEACHER or STUDENT
 * session renders ONLY the permission guidance — zero admin API calls
 * fire (the plan's step-1 privilege test; the backend's
 * `require_admin_actor` stays the authority, this gate is the
 * no-wasted-requests mirror). Anonymous visitors get the staff-login
 * CTA; ACTIVE-account and confirmed-TOTP enforcement stays server-side.
 *
 * The nav lists the six operational pages (users/whitelist/rewards/
 * redemptions/audit/system); the anonymous-reveal entry deliberately
 * does NOT live here — it hangs off the teacher workspace's community
 * moderation context where the comment rows are.
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { SectionError } from "@/components/ui/sectionStates";
import { StaffMenuSheet } from "@/components/shell/StaffMenuSheet";
import { WorkspaceSidebar } from "@/components/shell/WorkspaceSidebar";
import {
  GiftIcon,
  InboxIcon,
  ReviewIcon,
  TasksIcon,
  UserIcon,
} from "@/components/shell/navIcons";
import { useSession } from "@/features/auth/session";
import { adminWorkspaceGate } from "@/features/auth/workspace";

const NAV_ITEMS = [
  { href: "/admin/users", label: "用户与账户", icon: <UserIcon /> },
  { href: "/admin/whitelist", label: "注册白名单", icon: <TasksIcon /> },
  { href: "/admin/rewards", label: "奖励目录", icon: <GiftIcon /> },
  { href: "/admin/redemptions", label: "兑换审核", icon: <ReviewIcon /> },
  { href: "/admin/audit", label: "审计日志", icon: <InboxIcon /> },
  { href: "/admin/system", label: "系统设置", icon: <TasksIcon /> },
] as const;

const SIDEBAR_GROUPS = [{ label: "管理后台", items: NAV_ITEMS }] as const;

export function AdminShell({ children }: { children: ReactNode }) {
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
              <h1 className="auth-title">管理后台</h1>
              <p className="auth-subtitle">运营管理需要管理员账号登录</p>
            </div>
            <Link className="btn btn-primary btn-block" href="/staff/login">
              前往员工登录
            </Link>
            <p className="auth-alt-action">
              <Link className="link" href="/login">
                返回学生入口
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

  // Role gate: the admin workspace mounts ONLY for ADMIN; TEACHER and
  // STUDENT sessions get permission guidance (design §10) — never the
  // pages, so no admin data request can fire from a non-admin session.
  const gate = adminWorkspaceGate(state.me.role);
  if (gate.kind !== "admin") {
    const isTeacher = gate.kind === "staff-guidance";
    return (
      <div className="app-shell">
        <div className="app-topbar">
          <div className="app-topbar-inner">
            <span className="app-brand">CampusQuest</span>
          </div>
        </div>
        <main className="app-main">
          <div className="alert alert-error" role="alert">
            <p>
              <span className="alert-marker" aria-hidden="true">!</span>
              管理后台仅对管理员开放，当前账号是{isTeacher ? "教师" : "学生"}账号。
            </p>
            <p>
              <Link className="link" href={gate.workspacePath}>
                {isTeacher ? "返回教师工作台" : "返回学生首页"}
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
        brand={{ href: "/admin/users", label: "CampusQuest" }}
        groups={SIDEBAR_GROUPS}
      />
      <div className="app-body">
        <header className="app-topbar">
          <div className="app-topbar-inner">
            <span className="app-brand">CampusQuest</span>
            <nav className="app-nav" aria-label="管理后台导航">
              {NAV_ITEMS.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={pathname.startsWith(item.href) ? "page" : undefined}
                >
                  {item.label}
                </Link>
              ))}
            </nav>
            <div className="app-topbar-actions">
              <span className="app-user" title={state.me.nickname}>
                {state.me.nickname}
                <span className="staff-role-tag">管理员</span>
              </span>
              <StaffMenuSheet label="管理后台菜单" items={NAV_ITEMS} />
            </div>
          </div>
        </header>
        <main className="app-main">{children}</main>
      </div>
    </div>
  );
}
