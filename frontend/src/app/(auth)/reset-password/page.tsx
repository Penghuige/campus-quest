import Link from "next/link";
import type { Metadata } from "next";

import { ResetTokenSpendForm } from "@/features/auth/ResetTokenSpendForm";

export const metadata: Metadata = {
  title: "重置密码 · CampusQuest",
  description: "使用管理员交付的一次性重置口令设置新密码",
};

interface ResetPasswordPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

/**
 * The §3.2 admin-issued token's PUBLIC consume page: ?token= prefills
 * (the admin's copyable link lands here); the token itself is the
 * authorization — no session, no identifier.
 */
export default async function ResetPasswordPage({ searchParams }: ResetPasswordPageProps) {
  const params = await searchParams;
  const raw = params.token;
  const token = typeof raw === "string" ? raw : "";

  return (
    <>
      <div className="auth-head">
        <h1 className="auth-title">重置密码</h1>
        <p className="auth-subtitle">输入管理员交付的一次性重置口令，设置新密码</p>
      </div>
      <ResetTokenSpendForm initialToken={token} />
      <p className="auth-alt-action">
        <Link className="link" href="/login">
          返回登录
        </Link>
      </p>
    </>
  );
}
