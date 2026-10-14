import Link from "next/link";
import type { Metadata } from "next";

import { LoginForm } from "@/features/auth/LoginForm";
import { Button } from "@/components/ui/button";

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
      {/* V1.1 §2.4: the staff entry as a visible secondary action — the
       * pre-V1.1 page had NO staff path at all; one visible button, not
       * footer text. */}
      <Button asChild variant="secondary" block>
        <Link href="/staff/login">员工登录</Link>
      </Button>
    </>
  );
}
