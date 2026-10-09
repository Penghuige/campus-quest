import type { Metadata } from "next";

import { StaffProfileView } from "@/features/auth/StaffProfileView";

export const metadata: Metadata = {
  title: "个人信息 · CampusQuest",
  description: "当前登录员工账号的个人信息",
};

/**
 * Teacher profile page (defect #17, QA 2026-10-03): the workspace's
 * own-account view, /me facts only (the shell gates the route on a
 * TEACHER session — students get the workspace guidance, not broken
 * controls). 注册日期 joins when the backend DTO carries created_at.
 */
export default function TeacherProfilePage() {
  return <StaffProfileView />;
}
