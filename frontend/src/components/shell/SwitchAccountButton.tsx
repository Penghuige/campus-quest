"use client";
/**
 * The shells' 切换账号 entry (student AND staff) (owner batch 2026-10-23): the SAME
 * logout contract as 退出登录 (session revoke + cookie clearing + the
 * cross-tab fence), landing on the unified login where the
 * recent-logins cards await. It sits NEXT TO the logout button, not in
 * place of it — switching and leaving are different intents.
 *
 * The remembered-device cookie (`cq_staff_device`) deliberately
 * survives logout (clear_session_cookies never touches it): a device
 * that remembered an account still one-click-lands through its card —
 * that IS the switch experience.
 */
import { Button } from "@/components/ui/button";
import { useLogoutAction } from "@/features/auth/LogoutSection";

export function SwitchAccountButton() {
  const { busy, handle } = useLogoutAction("/login");
  return (
    <Button variant="ghost" onClick={handle} disabled={busy} aria-busy={busy}>
      切换账号
    </Button>
  );
}
