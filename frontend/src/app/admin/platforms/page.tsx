import type { Metadata } from "next";

import { PlatformsAdmin } from "@/features/platforms/PlatformsAdmin";

export const metadata: Metadata = {
  title: "平台管理 · CampusQuest 管理后台",
  description: "任务平台的注册表管理：新增、改名、排序与启停（无删除）",
};

/**
 * The platform registry admin page (platform-registry spec §4): adding
 * a platform is an admin-panel operation — zero migrations, zero
 * releases.
 */
export default function AdminPlatformsPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">平台管理</h1>
        <p className="page-subtitle">
          任务平台的注册表：新增平台无需发版；代码不可改，被引用平台只启停不删除
        </p>
      </div>
      <PlatformsAdmin />
    </>
  );
}
