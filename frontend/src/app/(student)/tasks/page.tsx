import type { Metadata } from "next";

import { TaskDiscovery } from "@/features/tasks/TaskDiscovery";

export const metadata: Metadata = {
  title: "任务广场 · CampusQuest",
  description: "浏览可领取的任务，查看奖励、截止时间与评分",
};

/** Task discovery (spec §42 Task Card): server shell + discovery island. */
export default function TasksPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">任务广场</h1>
        <p className="page-subtitle">领取任务单元前，可以先看任务卡信息</p>
      </div>
      <TaskDiscovery />
    </>
  );
}
