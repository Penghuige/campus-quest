import type { Metadata } from "next";

import { DashboardView } from "@/features/dashboard/DashboardView";

export const metadata: Metadata = {
  title: "我的主页 · CampusQuest",
  description: "需要处理的任务、积分与奖励进度、月度排名",
};

/**
 * Student dashboard (spec §42). The shell (layout) already gates on the
 * session; this page composes the dashboard island, whose sections each
 * own their loading/empty/error states.
 */
export default function StudentHomePage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">我的主页</h1>
        <p className="page-subtitle">需要处理的任务、积分进度与排名</p>
      </div>
      <DashboardView />
    </>
  );
}
