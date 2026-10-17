import type { Metadata } from "next";

import { AdminUserDetail } from "@/features/admin/AdminUserDetail";

export const metadata: Metadata = {
  title: "用户详情 · CampusQuest 管理后台",
  description: "账号全景与管理动作（代重置密码 / 会话管理 / 角色升降）",
};

/**
 * The user-detail route (V1.1 spec §3.1, Wave 2a): the users listing's
 * rows land here. The id rides the path (server-rendered head + the
 * client island's own aggregate fetch — the detail is live state, not
 * a static snapshot passed through search params).
 */
export default async function AdminUserDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">用户详情</h1>
        <p className="page-subtitle">
          账号全景与管理动作；每个动作都会写入审计日志
        </p>
      </div>
      <AdminUserDetail userId={id} />
    </>
  );
}
