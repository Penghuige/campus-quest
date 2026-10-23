"use client";
/**
 * The shells' 切换账号 entry (student AND staff) — popover batch (owner
 * feedback 2026-10-23): the click opens an IN-PLACE account menu
 * instead of hopping to the login page. The menu lists the device's
 * recent logins EXCLUDING the current account (the jar's head — see
 * `switchAccount.switchMenuEntries`); picking one stores the handoff
 * (`cq.switch-pick`, sessionStorage — an URL query would put the
 * identifier into the browser history) and takes the SAME logout
 * contract as 退出登录 (session revoke + cookie clearing + the
 * cross-tab fence), landing /login which auto-drives the picked card
 * through the #128 smart branch (remembered fast-landing / auto-send /
 * the password hand-back). 使用其他账号 — and an empty menu (no other
 * account on this device) — keep the original direct hop.
 *
 * The remembered-device cookie (`cq_staff_device`) deliberately
 * survives logout (clear_session_cookies never touches it): a device
 * that remembered an account still one-click-lands through its card.
 *
 * The menu is a lightweight labeled popover (the login page's
 * `.login-recent` pattern — native buttons a screen reader walks, no
 * menu-role arrow-key contract), NOT a Dialog: it anchors to the
 * topbar's action row, a fixed scrim catches outside clicks, Escape
 * closes and returns focus to the trigger, and opening hands focus to
 * the first item. It is a transient floating layer — deliberately
 * outside the pixel baselines (the trigger button itself is the #134
 * face and does not change).
 */
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useLogoutAction } from "@/features/auth/LogoutSection";
import { RecentLoginCardButton } from "@/features/auth/RecentLoginCard";
import { listRecentLogins, type RecentLoginEntry } from "@/features/auth/recentLogins";
import { setSwitchPick, switchMenuEntries } from "@/features/auth/switchAccount";

export function SwitchAccountButton() {
  const { busy, handle } = useLogoutAction("/login");
  const [entries, setEntries] = useState<RecentLoginEntry[] | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // Escape closes the menu and returns focus to the trigger (the
  // keyboard contract; the scrim already covers pointer-outsides).
  useEffect(() => {
    if (entries === null) {
      return;
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setEntries(null);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [entries]);

  // The opened menu hands focus to its first item — the keyboard user
  // lands where the work is (the code-input focus pattern).
  useEffect(() => {
    if (entries !== null) {
      menuRef.current?.querySelector<HTMLElement>("button")?.focus();
    }
  }, [entries]);

  function closeMenu() {
    setEntries(null);
    triggerRef.current?.focus();
  }

  function onToggle() {
    if (busy) {
      return;
    }
    if (entries !== null) {
      closeMenu();
      return;
    }
    // The jar read happens ON CLICK, never during render —
    // localStorage is a browser-only source (the hydration rule; the
    // login page reads the same jar on a deferred tick).
    const menu = switchMenuEntries(listRecentLogins());
    if (menu.length === 0) {
      // No other account on this device: the direct hop is the
      // honest fallback (the owner-batch v1 behavior).
      void handle();
      return;
    }
    setEntries(menu);
  }

  async function onPick(entry: RecentLoginEntry) {
    setEntries(null);
    setSwitchPick(entry.identifier);
    await handle();
  }

  function useOtherAccount() {
    setEntries(null);
    void handle();
  }

  return (
    <span className="switch-anchor">
      <Button
        ref={triggerRef}
        variant="ghost"
        onClick={onToggle}
        disabled={busy}
        aria-busy={busy}
        aria-expanded={entries !== null}
      >
        切换账号
      </Button>
      {entries !== null ? (
        <>
          <div className="switch-scrim" onClick={closeMenu} aria-hidden="true" />
          <div className="switch-menu" ref={menuRef} aria-label="切换账号">
            <p className="switch-menu-title">切换账号</p>
            <div className="switch-menu-cards">
              {entries.map((entry) => (
                <div className="login-recent-card" key={entry.identifier}>
                  <RecentLoginCardButton
                    entry={entry}
                    onPick={(picked) => {
                      void onPick(picked);
                    }}
                  />
                </div>
              ))}
            </div>
            <button type="button" className="switch-menu-other" onClick={useOtherAccount}>
              使用其他账号
            </button>
          </div>
        </>
      ) : null}
    </span>
  );
}
