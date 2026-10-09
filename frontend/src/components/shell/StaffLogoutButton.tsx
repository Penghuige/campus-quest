"use client";
/**
 * Defect #18 (QA 2026-10-03): the staff shells' topbar logout entry —
 * until now a logged-in teacher/admin could not free the device for
 * another account at all. Same logout() contract as the student
 * profile section (epoch bump + cache invalidation + cross-tab
 * fence); the topbar slot keeps it one click away, next to the menu
 * sheet.
 */
import { Button } from "@/components/ui/button";
import { useLogoutAction } from "@/features/auth/LogoutSection";

export function StaffLogoutButton() {
  const { busy, handle } = useLogoutAction("/staff/login");
  return (
    <Button variant="ghost" onClick={handle} disabled={busy} aria-busy={busy}>
      退出登录
    </Button>
  );
}
