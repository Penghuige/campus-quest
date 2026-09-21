import Link from "next/link";
import type { Metadata } from "next";

import { LoginForm } from "@/features/auth/LoginForm";

export const metadata: Metadata = {
  title: "登录 · CampusQuest",
  description: "使用学号和密码登录 CampusQuest",
};

interface LoginPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export default async function LoginPage({ searchParams }: LoginPageProps) {
  const params = await searchParams;
  const registered = params.registered === "1";
  const reset = params.reset === "1";

  return (
    <>
      <div className="auth-head">
        <h1 className="auth-title">登录 CampusQuest</h1>
        <p className="auth-subtitle">使用学号和密码登录</p>
      </div>
      {registered ? (
        <div className="alert alert-success" role="status">
          <p>注册成功，请使用学号登录。</p>
        </div>
      ) : null}
      {reset ? (
        <div className="alert alert-success" role="status">
          <p>密码已重置，请使用新密码登录。</p>
        </div>
      ) : null}
      <LoginForm />
      <p className="auth-alt-action">
        还没有账号？
        <Link className="link" href="/register">
          注册学生账号
        </Link>
      </p>
      <p className="auth-alt-action">
        <Link className="link" href="/forgot-password">
          忘记密码？
        </Link>
      </p>
    </>
  );
}
