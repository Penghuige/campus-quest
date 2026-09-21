import Link from "next/link";
import type { Metadata } from "next";

import { StaffLoginForm } from "@/features/auth/StaffLoginForm";

export const metadata: Metadata = {
  title: "员工登录 · CampusQuest",
  description: "使用员工邮箱、密码和动态口令登录 CampusQuest",
};

/**
 * Staff login (spec §5.8): its OWN route and form — the staff email
 * identifier never merges into the student student-number login
 * (task brief step 3: no ambiguous identifier parser).
 */
export default function StaffLoginPage() {
  return (
    <>
      <div className="auth-head">
        <h1 className="auth-title">员工登录</h1>
        <p className="auth-subtitle">使用员工邮箱、密码和动态验证码登录</p>
      </div>
      <StaffLoginForm />
      <p className="auth-alt-action">
        <Link className="link" href="/login">
          学生入口：使用学号登录
        </Link>
      </p>
    </>
  );
}
