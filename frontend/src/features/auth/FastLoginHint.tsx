"use client";
/**
 * The unified login page's LAYER-0 banner (spec §4; owner feedback ③
 * 2026-10-17; the probe lifted to the PANEL 2026-10-21 so one anonymous
 * read feeds both the banner and the cards' one-click branch): a
 * remembered device answers its masked identifier and the banner
 * offers the one-click continuation; the panel hands `mask` down —
 * null renders NOTHING (无 cookie 时登录页零多余元素).
 *
 * A click rides the cookie-only EMPTY-body verify (spec §3.1's read
 * path): authenticated lands by role through the shared readMe seam;
 * totp_required — a remembered ADMIN, the cookie never exempts the
 * second factor — hands the pending leg UP to the page's TOTP layer;
 * a refusal (the probe's answer went stale mid-session) swaps the
 * offer for the honest code-form guidance, never an error state.
 */
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useSession } from "@/features/auth/session";

import { fastLoginCookieOnly, readMe } from "./api";
import { landAfterStaffLogin } from "./staffLoginFlow";

export function FastLoginHint({
  mask,
  onTotpRequired,
}: {
  /** The panel's ONE probe answer — null means no remembered device. */
  mask: string | null;
  onTotpRequired: (pendingToken: string) => void;
}) {
  const router = useRouter();
  const { refresh: refreshSession } = useSession();
  const [busy, setBusy] = useState(false);
  const [stale, setStale] = useState(false);

  if (mask === null) {
    return null; // the zero-extra-elements login page
  }

  if (stale) {
    return (
      <p className="field-hint login-fast-stale" role="status">
        该设备尚未记住或已过期，请使用下方验证码登录。
      </p>
    );
  }

  async function onContinue() {
    if (busy) {
      return;
    }
    setBusy(true);
    try {
      const outcome = await fastLoginCookieOnly();
      if (outcome.kind === "totp_required") {
        onTotpRequired(outcome.pendingToken);
        return;
      }
      await landAfterStaffLogin({
        login: async () => undefined,
        refreshSession,
        router,
        readMe,
      });
    } catch {
      setStale(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-fast-hint">
      <p>
        继续以 <span className="mono">{mask}</span> 登录
      </p>
      <Button
        variant="secondary"
        onClick={() => void onContinue()}
        disabled={busy}
        aria-busy={busy}
      >
        {busy ? <span className="spinner" aria-hidden="true" /> : null}
        <span>一键继续</span>
      </Button>
    </div>
  );
}
