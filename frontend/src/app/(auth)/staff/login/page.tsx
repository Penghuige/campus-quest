import Link from "next/link";
import type { Metadata } from "next";

import { StaffEmailCodeLogin } from "@/features/auth/StaffEmailCodeLogin";
import { StaffLoginForm } from "@/features/auth/StaffLoginForm";
import { StaffLoginPanel } from "@/features/auth/StaffLoginPanel";
import { StaffMobileCodeLogin } from "@/features/auth/StaffMobileCodeLogin";

export const metadata: Metadata = {
  title: "员工登录 · CampusQuest",
  description: "使用员工邮箱验证码、手机验证码或密码登录 CampusQuest",
};

/**
 * Staff login (spec §5.8; V1.1 §2.1): its OWN route — the staff email
 * identifier never merges into the student student-number login (task
 * brief step 3: no ambiguous identifier parser). V1.1 makes the
 * emailed 6-digit code the DEFAULT channel; the mobile-code channel
 * (login-iteration item ①: the staff account's BOUND phone) and the
 * password+TOTP form sit one visible toggle away (spec §2.4
 * compatibility).
 */
export default function StaffLoginPage() {
  return (
    <>
      <div className="auth-head">
        <h1 className="auth-title">员工登录</h1>
        <p className="auth-subtitle">使用员工邮箱验证码登录；也可切换手机验证码或密码登录</p>
      </div>
      <StaffLoginPanel
        codeChannel={<StaffEmailCodeLogin />}
        mobileChannel={<StaffMobileCodeLogin />}
        passwordChannel={<StaffLoginForm />}
      />
      <p className="auth-alt-action">
        <Link className="link" href="/login">
          学生入口：使用学号登录
        </Link>
      </p>
    </>
  );
}
