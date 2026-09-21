import type { ReactNode } from "react";

/**
 * Auth route-group shell: a Server Component frame (patterns §2) that
 * centers the card; every page inside keeps its own heading + Client form
 * island. Authentication pages stay visually serious (design system §2).
 */
export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <main className="auth-shell">
      <div className="auth-card">{children}</div>
    </main>
  );
}
