import type { Metadata } from "next";

import { InvitationListPanel } from "@/features/admin/InvitationListPanel";
import { StaffInvitations } from "@/features/admin/StaffInvitations";

export const metadata: Metadata = {
  title: "员工邀请 · CampusQuest 管理后台",
  description: "发放与管理一次性邀请链接（48 小时有效，仅显示一次；可撤销、可重发）",
};

/**
 * Staff-invitation issuance page (spec §5.8 step 1 + V1.1 §4.2 Wave 2a):
 * the create form stays; the lifecycle panel below it lists every
 * invitation with its four-state verdict and the 撤销/重发 verbs
 * (a resend mints a fresh one-time link and kills the old token).
 */
export default function AdminInvitationsPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">员工邀请</h1>
        <p className="page-subtitle">
          发放一次性邀请链接，邀请教师或管理员加入；链接 48 小时内有效，创建后只显示一次
        </p>
      </div>
      <StaffInvitations />
      <InvitationListPanel />
    </>
  );
}
