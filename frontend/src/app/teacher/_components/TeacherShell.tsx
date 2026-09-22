"use client";
/**
 * Staff shell for the /teacher workspace (design §8 Teacher; patterns §2).
 *
 * The session gate REUSES the student shell's `useSession` (SWR-style /me
 * cache) with a STAFF rule: TEACHER/ADMIN pass, a STUDENT session gets
 * the permission-denied panel (design §10: explain + navigate back, not a
 * page of failing sections), anonymous visitors get the staff-login CTA.
 * ACTIVE-account and confirmed-TOTP enforcement stays with the backend's
 * `require_staff_management_actor` — a 403 surfaces through each
 * section's error state, never a client-side guess.
 *
 * The nav is deliberately distinct from the student shell: review queue
 * first (the workbench's primary loop), then task management. Per-task
 * surfaces (import, statistics, community moderation) hang off the task
 * detail, so no separate nav entries exist for them.
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { SectionError } from "@/components/ui/sectionStates";
import { useSession } from "@/features/auth/session";
import { teacherWorkspaceGate } from "@/features/auth/workspace";

const NAV_ITEMS = [
  { href: "/teacher/reviews", label: "审核队列" },
  { href: "/teacher/tasks", label: "任务管理" },
] as const;

export function TeacherShell({ children }: { children: ReactNode }) {
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
              <h1 className="auth-title">员工工作台</h1>
              <p className="auth-subtitle">任务管理与提交审核需要员工账号登录</p>
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

  // Role gate (`workspace.ts`, PR #4 hardening Task 3): the staff
  // workspace mounts ONLY for TEACHER/ADMIN; a STUDENT session gets the
  // permission guidance (design §10: explain + navigate back), never a
  // page of 403ing sections.
  const gate = teacherWorkspaceGate(state.me.role);
  if (gate.kind === "student-guidance") {
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
              教师工作台仅对教师与管理员开放，当前账号是学生账号。
            </p>
            <p>
              <Link className="link" href={gate.workspacePath}>
                返回学生首页
              </Link>
            </p>
          </div>
        </main>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <header className="app-topbar">
        <div className="app-topbar-inner">
          <span className="app-brand">CampusQuest</span>
          <nav className="app-nav" aria-label="教师工作台导航">
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
              <span className="staff-role-tag">
                {state.me.role === "ADMIN" ? "管理员" : "教师"}
              </span>
            </span>
          </div>
        </div>
      </header>
      <main className="app-main">{children}</main>
    </div>
  );
}
