import Link from "next/link";
import type { Metadata } from "next";

import { RegisterForm } from "@/features/auth/RegisterForm";

export const metadata: Metadata = {
  title: "注册 · CampusQuest",
  description: "注册 CampusQuest 学生账号（需要白名单学号和手机验证码）",
};

export default function RegisterPage() {
  return (
    <>
      <div className="auth-head">
        <h1 className="auth-title">注册学生账号</h1>
        <p className="auth-subtitle">使用白名单内的学号注册，手机号用于验证</p>
      </div>
      <RegisterForm />
      <p className="auth-alt-action">
        已有账号？
        <Link className="link" href="/login">
          直接登录
        </Link>
      </p>
    </>
  );
}
