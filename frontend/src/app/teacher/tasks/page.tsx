import type { Metadata } from "next";

import { TeacherTasksView } from "@/features/admin/TeacherTasksView";

export const metadata: Metadata = {
  title: "任务管理 · CampusQuest",
  description: "管理我创建与协作的任务：新建、导入任务单元、发布与生命周期操作",
};

/**
 * Teacher workbench task list (spec §41): server shell + workbench
 * island. The staff guard lives in the route-group layout.
 */
export default function TeacherTasksPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">任务管理</h1>
        <p className="page-subtitle">我创建与协作的任务，含草稿与全部状态</p>
      </div>
      <TeacherTasksView />
    </>
  );
}
