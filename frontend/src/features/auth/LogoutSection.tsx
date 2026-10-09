"use client";
/**
 * Defect #11 (QA 2026-10-03): the student profile's explicit logout
 * entry — the account-actions section the 个人信息 tab lacked. The
 * action rides the session module's full logout() contract (server
 * revoke + epoch bump + cache invalidation + the cross-tab context
 * fence), never a bare cookie clear: sibling tabs must drop the
 * account too, and whoever logs in next on this device must never
 * inherit this context's in-flight intent.
 */
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";

import { logout } from "./api";
import { SettingsSection } from "./AccountSettings";

/**
 * Run the full logout contract, then land on the login surface.
 * Shared by the student profile section and the staff shells' topbar
 * button — one semantics, two entries (defects #11/#18 are the same
 * gap on two surfaces).
 */
export function useLogoutAction(destination: "/login" | "/staff/login") {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const handle = async () => {
    setBusy(true);
    try {
      await logout();
      router.replace(destination);
    } finally {
      setBusy(false);
    }
  };
  return { busy, handle };
}

/** The 个人信息 tab's account-actions block (below the edit forms). */
export function LogoutSection() {
  const { busy, handle } = useLogoutAction("/login");
  return (
    <SettingsSection
      title="账号操作"
      hint="退出当前设备上的登录状态；下次使用需重新登录。"
    >
      <Button
        variant="danger"
        onClick={handle}
        disabled={busy}
        aria-busy={busy}
      >
        退出登录
      </Button>
    </SettingsSection>
  );
}
