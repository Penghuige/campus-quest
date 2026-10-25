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
 *
 * Chrome verdict (backlog UX: the refresh shell jump): while the
 * session resolves, the pre-resolve frames (loading, error) render the
 * workspace geometry OPTIMISTICALLY when the server HTML carried
 * session evidence (`shellChromeFor` + the layout's cookie probe), so
 * a reload never repaints a different shell for the ~0.6s resolve
 * window. The user-data slots (rail account, topbar actions) render
 * as placeholders; the notification bell does NOT mount until the
 * session answers (no speculative polling).
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { SectionError } from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { BottomNav } from "@/components/shell/BottomNav";
import { SwitchAccountButton } from "@/components/shell/SwitchAccountButton";
import { WorkspaceSidebar, navItemActive, type SideNavItem } from "@/components/shell/WorkspaceSidebar";
import { BellIcon, GiftIcon, HomeIcon, InboxIcon, ReviewIcon, TasksIcon, TrophyIcon, UserIcon } from "@/components/shell/navIcons";
import { useSession, type MeProfile } from "@/features/auth/session";
import { NicknameNudge } from "@/features/auth/NicknameNudge";
import { ApprovalPendingBanner } from "@/features/auth/ApprovalPendingBanner";
import { shellChromeFor, type ShellHint } from "@/features/auth/shellChrome";
import { StaffRouteRedirect } from "@/features/auth/StaffRouteRedirect";
import { studentWorkspaceGate } from "@/features/auth/workspace";
import { OPERATIONS_NAV } from "@/features/innovation/InnovationNavigation";
import { getInnovationCapabilities } from "@/features/innovation/operationsApi";
import { NotificationBell } from "@/features/notifications/NotificationBell";
import { getAuthEpoch } from "@/lib/accessToken";

/* Plan 11 Task 3 — the frozen navigation contract (route-true
 * amendment): desktop sidebar primary + secondary, narrow bottom nav.
 * 我的任务 stays anchored on the dashboard (its only list surface);
 * 社区 stays anchored on task detail — V1 has neither route.
 * Innovation batch 1 adds the 创新创新创业 entry to the rail and the
 * medium top nav (PR #43's INNOVATION_NAV position, ReviewIcon); the
 * narrow BottomNav keeps its fixed 5 slots by owner decision — the
 * campus shell's own top nav covers the narrow band there. */
const SIDEBAR_PRIMARY = [
  { href: "/", label: "首页", icon: <HomeIcon /> },
  { href: "/tasks", label: "任务", icon: <TasksIcon /> },
  { href: "/innovation", label: "创新创业", icon: <ReviewIcon /> },
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
  { href: "/innovation", label: "创新创业" },
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

/* Audit #4: one grapheme-safe initial helper (nicknames are validated
 * per grapheme cluster; Array.from splits ZWJ emoji in half). */
function nicknameInitial(nickname: string): string {
  const segmenter = new Intl.Segmenter("zh-CN", { granularity: "grapheme" });
  const first = segmenter.segment(nickname)[Symbol.iterator]().next();
  return first.done ? "同" : first.value.segment;
}

/** The medium band's horizontal top nav — shared by the authenticated
 * shell and the optimistic pre-resolve frame so the two cannot drift. */
function MediumTopNav({ pathname, extra = [] }: { pathname: string; extra?: readonly SideNavItem[] }) {
  const items = extra.length ? [...MEDIUM_NAV, ...extra] : MEDIUM_NAV;
  return (
    <nav className="app-nav" aria-label="主导航">
      {items.map((item) => (
        <Link
          key={item.href}
          href={item.href}
          aria-current={navItemActive(pathname, item.href) ? "page" : undefined}
        >
          {item.label}
        </Link>
      ))}
    </nav>
  );
}

/** The rail footer's account slot while the session resolves: the
 * real avatar + nickname are user data, so the optimistic frame holds
 * their geometry with rail-toned placeholders instead. */
function RailAccountPlaceholder() {
  return (
    <div className="rail-account" data-loading="true" aria-hidden="true">
      <span className="rail-avatar skeleton" />
      <span className="rail-account-name-slot skeleton skeleton-line" />
    </div>
  );
}

/**
 * The optimistic workspace frame: the authenticated shell's geometry
 * (rail, degraded topbar strip with its actions pill, bottom nav)
 * with the session-dependent slots as placeholders. Serves the
 * loading frame (skeleton content) and the session-outage frame
 * (retryable error card) — a resolve or a retry then settles into
 * the authenticated shell WITHOUT a layout jump.
 */
function OptimisticWorkspaceShell({
  state,
  main,
}: {
  state: "loading" | "error";
  main: ReactNode;
}) {
  const pathname = usePathname();
  return (
    <div className="app-shell" data-shell={state}>
      <WorkspaceSidebar
        brand={{ href: "/", label: "CampusQuest" }}
        groups={SIDEBAR_GROUPS}
        footer={<RailAccountPlaceholder />}
      />
      <div className="app-body">
        <header className="app-topbar">
          <div className="app-topbar-inner">
            <span className="app-brand">CampusQuest</span>
            <MediumTopNav pathname={pathname} />
            <div className="app-topbar-actions" aria-hidden="true">
              {/* The bell's glyph at rest — the real NotificationBell
               * polls, so it mounts only once the session answers. */}
              <span className="topbar-bell">
                <span className="bell-glyph">
                  <BellIcon />
                </span>
              </span>
              <span className="avatar-chip skeleton" />
            </div>
          </div>
        </header>
        <main className="app-main" aria-busy={state === "loading" ? "true" : undefined}>
          {main}
        </main>
      </div>
      <BottomNav items={BOTTOM_NAV} />
    </div>
  );
}

/** The minimal single-column shell: the chrome for visitors WITHOUT
 * session evidence (the pre-resolve frames) and for everyone the
 * workspace is not for (anonymous card, staff guidance). */
function MinimalShell({ state, children }: { state: string; children?: ReactNode }) {
  return (
    <div className="app-shell" data-shell={state}>
      <div className="app-topbar">
        <div className="app-topbar-inner">
          <span className="app-brand">CampusQuest</span>
        </div>
      </div>
      <main className="app-main" aria-busy={state === "loading" ? "true" : undefined}>
        {children}
      </main>
    </div>
  );
}

export function StudentShell({
  children,
  sessionCookiePresent,
  shellHint,
}: {
  children: ReactNode;
  /** The layout's server-side probe: did the document request carry
   * the readable csrf cookie (session evidence)? */
  sessionCookiePresent: boolean;
  /** The mint's coarse role hint (cq_shell), when present — routes a
   * STAFF session's pre-resolve frames to the minimal chrome (their
   * terminal geometry here) instead of the student workspace. */
  shellHint: ShellHint | undefined;
}) {
  const { state, refresh } = useSession();
  const pathname = usePathname();

  if (state.status === "loading") {
    if (shellChromeFor("loading", sessionCookiePresent, shellHint) === "workspace") {
      return (
        <OptimisticWorkspaceShell
          state="loading"
          main={
            <>
              <span className="skeleton skeleton-line" style={{ width: "40%" }} />
              <span className="skeleton skeleton-line" />
              <span className="skeleton skeleton-block" />
            </>
          }
        />
      );
    }
    if (shellHint === "staff") {
      // The staff redirect placeholder: the hint routes the pre-resolve
      // frame to minimal geometry carrying the SAME quiet copy the
      // redirect island shows — one placeholder across the whole jump.
      return (
        <MinimalShell state="loading">
          <p className="field-hint" role="status">
            正在前往工作台…
            <span className="spinner" aria-hidden="true" />
          </p>
        </MinimalShell>
      );
    }
    return (
      <MinimalShell state="loading">
        <span className="skeleton skeleton-line" style={{ width: "40%" }} />
        <span className="skeleton skeleton-line" />
        <span className="skeleton skeleton-block" />
      </MinimalShell>
    );
  }

  if (state.status === "anonymous") {
    return (
      <div className="app-shell" data-shell="anonymous">
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
    const errorCard = <SectionError error={state.error} onRetry={refresh} retryLabel="重新加载" />;
    if (shellChromeFor("error", sessionCookiePresent, shellHint) === "workspace") {
      return <OptimisticWorkspaceShell state="error" main={errorCard} />;
    }
    if (shellHint === "staff") {
      // A /me failure under a staff hint: the error frame lives INSIDE
      // the placeholder shell — a retry AND a way out (the session may
      // be dead), never an infinite 正在前往.
      return (
        <MinimalShell state="error">
          {errorCard}
          <p className="auth-alt-action">
            <Link className="link" href="/login">
              前往员工登录
            </Link>
          </p>
        </MinimalShell>
      );
    }
    return <MinimalShell state="error">{errorCard}</MinimalShell>;
  }

  // Role gate (PR #4 hardening Task 3): the student workspace mounts
  // ONLY for a STUDENT session. A TEACHER/ADMIN session is REDIRECTED
  // to its own workspace (audit item A, owner feedback 2026-10-15 —
  // the guidance card is retired for this family; true cross-role
  // errors keep their cards in the staff shells) — and because the
  // pages (children) never mount, none of the student-only sections
  // can fire their API calls from a staff session.
  const gate = studentWorkspaceGate(state.me.role);
  if (gate.kind !== "student") {
    return (
      <MinimalShell state="gate">
        <StaffRouteRedirect role={state.me.role} />
      </MinimalShell>
    );
  }

  return (
    <AuthenticatedWorkspaceShell
      key={`${state.me.id}:${getAuthEpoch()}`}
      me={state.me}
      pathname={pathname}
    >
      {children}
    </AuthenticatedWorkspaceShell>
  );
}

/**
 * The authenticated student workspace. It mounts only once the session
 * has answered, so the capabilities query below loads under the settled
 * account (and the key remount above re-reads it after every account
 * change). The operations entry stays hidden until the backend says the
 * account holds an operations grant — availability is server-authoritative
 * (design §10), so a grant-less student never sees a dead lead.
 */
function AuthenticatedWorkspaceShell({
  me,
  pathname,
  children,
}: {
  me: MeProfile;
  pathname: string;
  children: ReactNode;
}) {
  const { state: capabilities, retry } = useSection(() =>
    me.status === "ACTIVE"
      ? getInnovationCapabilities().then((data) => data.operations_enabled)
      : Promise.resolve(false),
  );
  const operationsEnabled = capabilities.status === "ready" && capabilities.data;
  const groups = operationsEnabled
    ? [...SIDEBAR_GROUPS, { label: "双创运营", items: [OPERATIONS_NAV] }]
    : SIDEBAR_GROUPS;
  return (
    <div className="app-shell" data-shell="workspace">
      <WorkspaceSidebar
        brand={{ href: "/", label: "CampusQuest" }}
        groups={groups}
        footer={
          // Review round 2: the account lives in the rail footer — a
          // purposeful bottom anchor for the dark rail, and one less
          // floating generic-admin element at the top.
          <Link className="rail-account" href="/profile" title="我的账户">
            <span className="rail-avatar" aria-hidden="true">
              {nicknameInitial(me.nickname)}
            </span>
            <span className="rail-account-name">{me.nickname}</span>
          </Link>
        }
      />
      <div className="app-body">
        <header className="app-topbar">
          <div className="app-topbar-inner">
            <span className="app-brand">CampusQuest</span>
            <MediumTopNav
              pathname={pathname}
              extra={operationsEnabled ? [OPERATIONS_NAV] : []}
            />
            <div className="app-topbar-actions">
              <SwitchAccountButton />
              <NotificationBell />
              <Link
                className="avatar-chip"
                href="/profile"
                aria-label="我的账户"
                title="我的账户"
              >
                <span aria-hidden="true">{nicknameInitial(me.nickname)}</span>
              </Link>
            </div>
          </div>
        </header>
        <main className="app-main">
          {capabilities.status === "error" ? (
            <SectionError
              error={capabilities.error}
              onRetry={retry}
              retryLabel="重新读取运营入口"
            />
          ) : null}
          <ApprovalPendingBanner me={me} />
          <NicknameNudge me={me} />
          {children}
        </main>
      </div>
      <BottomNav items={BOTTOM_NAV} />
    </div>
  );
}
