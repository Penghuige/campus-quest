import type { Metadata } from "next";

import { TaskDetailView } from "@/features/tasks/TaskDetailView";

export const metadata: Metadata = {
  title: "任务详情 · CampusQuest",
  description: "任务说明、奖励与截止信息，以及领取入口",
};

interface TaskDetailPageProps {
  params: Promise<{ taskId: string }>;
}

/** Task detail (patterns §8 archetype): dynamic segment -> detail island. */
export default async function TaskDetailPage({ params }: TaskDetailPageProps) {
  const { taskId } = await params;
  return <TaskDetailView taskId={taskId} />;
}
