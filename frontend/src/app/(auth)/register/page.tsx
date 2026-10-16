import Link from "next/link";
import type { Metadata } from "next";

import { RegisterForm } from "@/features/auth/RegisterForm";

export const metadata: Metadata = {
  title: "注册 · CampusQuest",
  description: "注册 CampusQuest 学生账号（学号 + 手机验证码，密码可选）",
};

export default function RegisterPage() {
  return (
    <>
      <div className="auth-head">
        <h1 className="auth-title">注册学生账号</h1>
        <p className="auth-subtitle">使用学号与手机验证码注册；注册后待管理员审核</p>
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
