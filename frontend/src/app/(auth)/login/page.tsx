import Link from "next/link";
import type { Metadata } from "next";

import { UnifiedLoginPanel } from "@/features/auth/UnifiedLoginPanel";

export const metadata: Metadata = {
  title: "登录 · CampusQuest",
  description: "使用手机号、邮箱或学号登录 CampusQuest",
};

interface LoginPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

/**
 * The UNIFIED login page (spec 2026-10-19 §4): student and staff
 * merge into ONE page — the remembered-device banner (conditional),
 * the single identifier field + code/password progressive disclosure,
 * the ADMIN TOTP leg, and the unregistered phone's inline register
 * all live in UnifiedLoginPanel. /staff/login redirects here; the
 * invitation accept flow stays its own route.
 */
export default async function LoginPage({ searchParams }: LoginPageProps) {
  const params = await searchParams;
  const reset = params.reset === "1";

  return (
    <>
      <div className="auth-head">
        <h1 className="auth-title">登录 CampusQuest</h1>
        <p className="auth-subtitle">使用手机号、邮箱或学号登录</p>
      </div>
      {reset ? (
        <div className="alert alert-success" role="status">
          <p>密码已重置，请使用新密码登录</p>
        </div>
      ) : null}
      <UnifiedLoginPanel />
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
