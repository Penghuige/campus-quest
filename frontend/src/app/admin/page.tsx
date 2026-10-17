import type { Metadata } from "next";

import { AdminDashboard } from "@/features/admin/AdminDashboard";

export const metadata: Metadata = {
  title: "管理台概览 · CampusQuest 管理后台",
  description: "用户、任务与审核队列的运营仪表盘",
};

/**
 * The admin workspace's landing (V1.1 spec §4.1, Wave 2a): the
 * dashboard replaces the old no-page /admin root — an admin's login
 * lands here (#84's routing reads /me, never a fixed redirect), and
 * the users directory stays one nav item away.
 */
export default function AdminDashboardPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">管理台概览</h1>
        <p className="page-subtitle">
          用户、任务状态与审核队列的当日运营总览；数字均为服务端权威口径
        </p>
      </div>
      <AdminDashboard />
    </>
  );
}
