import Link from "next/link";
import type { Metadata } from "next";

import { PasswordResetForm } from "@/features/auth/PasswordResetForm";

export const metadata: Metadata = {
  title: "找回密码 · CampusQuest",
  description: "通过绑定手机号的验证码重置 CampusQuest 密码",
};

export default function ForgotPasswordPage() {
  return (
    <>
      <div className="auth-head">
        <h1 className="auth-title">找回密码</h1>
        <p className="auth-subtitle">通过绑定手机号的验证码重置密码</p>
      </div>
      <PasswordResetForm />
      <p className="auth-alt-action">
        想起密码了？
        <Link className="link" href="/login">
          返回登录
        </Link>
      </p>
    </>
  );
}
