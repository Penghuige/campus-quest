import Link from "next/link";
import type { Metadata } from "next";

import { StaffLoginForm } from "@/features/auth/StaffLoginForm";
import { StaffLoginPanel } from "@/features/auth/StaffLoginPanel";
import { StaffUnifiedCodeLogin } from "@/features/auth/StaffUnifiedCodeLogin";

export const metadata: Metadata = {
  title: "员工登录 · CampusQuest",
  description: "使用员工邮箱或手机号验证码登录，也可切换密码登录",
};

/**
 * Staff login (spec §5.8 + the two-channel convergence 2026-10-20):
 * its OWN route — the staff identifier never merges into the student
 * student-number login. The UNIFIED code channel (one identifier
 * field, email or phone) is the default; the password+TOTP form sits
 * one visible toggle away (spec §2.4 compatibility). The page rides
 * the same ChannelToggle structure as the student login.
 */
export default function StaffLoginPage() {
  return (
    <>
      <div className="auth-head">
        <h1 className="auth-title">员工登录</h1>
        <p className="auth-subtitle">使用员工邮箱或手机号登录</p>
      </div>
      <StaffLoginPanel
        codeChannel={<StaffUnifiedCodeLogin />}
        passwordChannel={<StaffLoginForm />}
      />
      <p className="auth-alt-action">
        <Link className="link" href="/login">
          学生入口：使用学号或手机号登录
        </Link>
      </p>
    </>
  );
}
