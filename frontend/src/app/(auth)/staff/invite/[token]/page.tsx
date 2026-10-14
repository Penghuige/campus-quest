import type { Metadata } from "next";

import { StaffInviteAccept } from "@/features/auth/StaffInviteAccept";

export const metadata: Metadata = {
  title: "接受员工邀请 · CampusQuest",
  description: "教师设置密码（可选）即完成激活；管理员设置密码并绑定动态口令（TOTP）",
};

interface StaffInvitePageProps {
  params: Promise<{ token: string }>;
}

/**
 * Staff invitation acceptance (spec §5.8 steps 1-5; V1.1 §2.3).
 *
 * Server shell only: the token route param flows straight into the client
 * island. The token is a single-use secret — it is never logged and never
 * rendered back to the user.
 */
export default async function StaffInvitePage({ params }: StaffInvitePageProps) {
  const { token } = await params;

  return (
    <>
      <div className="auth-head">
        <h1 className="auth-title">接受员工邀请</h1>
        <p className="auth-subtitle">教师设置密码（可选）即完成激活；管理员需绑定动态口令</p>
      </div>
      <p className="auth-alt-action">邀请链接仅可使用一次，请一次性完成激活。</p>
      <StaffInviteAccept token={token} />
    </>
  );
}
