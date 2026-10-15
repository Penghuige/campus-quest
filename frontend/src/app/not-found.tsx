import Link from "next/link";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "页面不存在 · CampusQuest",
};

/**
 * The custom 404 (audit item B; owner feedback 2026-10-15): the product
 * speaks Chinese everywhere except the router's default English
 * not-found page. Minimal shell, the honest copy, and a way home —
 * the home link re-enters the role-aware shell flow (a staff visitor
 * is redirected onward from there by the audit-item-A fix).
 */
export default function NotFound() {
  return (
    <div className="app-shell" data-shell="not-found">
      <header className="app-topbar">
        <div className="app-topbar-inner">
          <span className="app-brand">CampusQuest</span>
        </div>
      </header>
      <main className="app-main">
        <div className="auth-card">
          <div className="auth-head">
            <h1 className="auth-title">页面不存在</h1>
            <p className="auth-subtitle">你访问的页面不存在或已被移动。</p>
          </div>
          <Link className="btn btn-primary btn-block" href="/">
            返回首页
          </Link>
        </div>
      </main>
    </div>
  );
}
