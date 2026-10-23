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
import { fetchRememberedHint, type RememberedSlot } from "@/features/auth/api";
import { listRecentLogins, type RecentLoginEntry } from "@/features/auth/recentLogins";
import { setSwitchPick, switchMenuEntries } from "@/features/auth/switchAccount";
import { matchRememberedSlot } from "@/features/auth/unifiedLoginView";

/** The open menu's model: the offerable rows plus the remembered-slot
 * probe answer that feeds the 免登录 badges. */
interface SwitchMenuState {
  rows: RecentLoginEntry[];
  slots: RememberedSlot[];
}

export function SwitchAccountButton() {
  const { busy, handle } = useLogoutAction("/login");
  const [menu, setMenu] = useState<SwitchMenuState | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // Escape closes the menu and returns focus to the trigger (the
  // keyboard contract; the scrim already covers pointer-outsides).
  useEffect(() => {
    if (menu === null) {
      return;
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenu(null);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [menu]);

  // The opened menu hands focus to its first item — the keyboard user
  // lands where the work is (the code-input focus pattern).
  useEffect(() => {
    if (menu !== null) {
      menuRef.current?.querySelector<HTMLElement>("button")?.focus();
    }
  }, [menu]);

  function closeMenu() {
    setMenu(null);
    triggerRef.current?.focus();
  }

  async function onToggle() {
    if (busy) {
      return;
    }
    if (menu !== null) {
      closeMenu();
      return;
    }
    // The jar read happens ON CLICK, never during render —
    // localStorage is a browser-only source (the hydration rule; the
    // login page reads the same jar on a deferred tick).
    const rows = switchMenuEntries(listRecentLogins());
    if (rows.length === 0) {
      // No other account on this device: the direct hop is the
      // honest fallback (the owner-batch v1 behavior).
      void handle();
      return;
    }
    // The 免登录 badges ride the remembered-slot probe (the multi-slot
    // batch): opened WITH the answer so the badges never pop in late.
    // The probe is fail-safe — any failure answers [] and the badges
    // simply stay hidden (the pick still works through the code flow).
    const slots = await fetchRememberedHint();
    setMenu({ rows, slots });
  }

  async function onPick(entry: RecentLoginEntry) {
    setMenu(null);
    setSwitchPick(entry.identifier);
    await handle();
  }

  function useOtherAccount() {
    setMenu(null);
    void handle();
  }

  return (
    <span className="switch-anchor">
      <Button
        ref={triggerRef}
        variant="ghost"
        onClick={() => {
          void onToggle();
        }}
        disabled={busy}
        aria-busy={busy}
        aria-expanded={menu !== null}
      >
        切换账号
      </Button>
      {menu !== null ? (
        <>
          <div className="switch-scrim" onClick={closeMenu} aria-hidden="true" />
          <div className="switch-menu" ref={menuRef} aria-label="切换账号">
            <p className="switch-menu-title">切换账号</p>
            <div className="switch-menu-cards">
              {menu.rows.map((entry) => (
                <div className="login-recent-card" key={entry.identifier}>
                  <RecentLoginCardButton
                    entry={entry}
                    onPick={(picked) => {
                      void onPick(picked);
                    }}
                  />
                  {matchRememberedSlot(entry.masked, menu.slots) !== null ? (
                    <span className="switch-menu-fast">免登录</span>
                  ) : null}
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
