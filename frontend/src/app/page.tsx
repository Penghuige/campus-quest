import type { Metadata } from "next";
import { cookies } from "next/headers";
import { CSRF_COOKIE_NAME } from "@/lib/csrf";
import { SHELL_HINT_COOKIE_NAME, shellHintFromCookieValue, shellRedirectsToLogin } from "@/features/auth/shellChrome";
import { LandingPage } from "@/features/landing/LandingPage";
import { StudentShell } from "./(student)/_components/StudentShell";

import { DashboardView } from "@/features/dashboard/DashboardView";

export const metadata: Metadata = {
  title: "智汇华师 · 校园协作与成长",
  description: "参与校园任务，寻找项目伙伴，展示技能与作品",
};

/**
 * Student dashboard (spec §42). The shell (layout) already gates on the
 * session; this page composes the dashboard island, whose sections each
 * own their loading/empty/error states.
 */
export default async function HomePage() {
  const cookieStore = await cookies();
  const sessionCookiePresent = cookieStore.has(CSRF_COOKIE_NAME);
  const shellHint = shellHintFromCookieValue(cookieStore.get(SHELL_HINT_COOKIE_NAME)?.value);
  // Evidence picks first-paint geometry only; useSession still resolves identity/role.
  if (shellRedirectsToLogin(sessionCookiePresent, shellHint)) return <LandingPage />;
  return (
    <StudentShell sessionCookiePresent={sessionCookiePresent} shellHint={shellHint} anonymousFallback={<LandingPage />}>
      <div className="page-head">
        <h1 className="page-title">我的主页</h1>
        <p className="page-subtitle">需要处理的任务、积分进度与排名</p>
      </div>
      <DashboardView />
    </StudentShell>
  );
}
