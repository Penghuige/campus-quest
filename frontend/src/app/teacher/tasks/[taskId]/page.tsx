import type { Metadata } from "next";

import { TeacherTaskDetailView } from "@/features/admin/TeacherTaskDetailView";

export const metadata: Metadata = {
  title: "任务工作台 · CampusQuest",
  description: "任务配置、任务单元导入、统计、协作者与社区管理",
};

interface TeacherTaskDetailPageProps {
  params: Promise<{ taskId: string }>;
}

/**
 * Workbench detail (spec §41): dynamic segment -> the detail island,
 * which owns the access-denied UX for unknown/unshared tasks (design
 * §10) and composes the four management surfaces.
 */
export default async function TeacherTaskDetailPage({
  params,
}: TeacherTaskDetailPageProps) {
  const { taskId } = await params;
  return <TeacherTaskDetailView taskId={taskId} />;
}
