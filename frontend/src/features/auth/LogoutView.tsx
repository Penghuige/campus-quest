"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { isApiError } from "@/lib/errors";
import { resolveAppPath } from "@/lib/appPath";
import { logout } from "./api";

/** Standalone page: private workspaces unmount before revocation starts. */
export function LogoutView() {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const inFlight = useRef(false);
  async function confirm() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15_000);
    try {
      await logout(controller.signal);
      // A full document transition drops Router caches as well as auth stores.
      window.location.replace(resolveAppPath("/login"));
    } catch (error) {
      // This endpoint returns this precise verdict only when there is no
      // refresh credential. A lost 204 may already have cleared the cookie.
      if (isApiError(error) && error.status === 401 && error.code === "AUTHENTICATION_REQUIRED") {
        window.location.replace(resolveAppPath("/login"));
        return;
      }
      setFailed(true);
      setBusy(false);
      inFlight.current = false;
    } finally {
      clearTimeout(timer);
    }
  }
  return <>
    <div className="auth-head"><h1 className="auth-title">退出当前账号</h1><p className="auth-subtitle">退出当前浏览器的登录会话，然后可换用另一个演示账号。</p></div>
    {failed ? <p className="alert alert-error" role="alert" aria-label="退出结果">退出尚未确认，可能是网络或服务暂时不可用。请重试；确认完成前不要将此浏览器交给他人。</p> : null}
    <Button block disabled={busy} onClick={() => void confirm()}>{busy ? "正在退出…" : failed ? "重试退出登录" : "确认退出登录"}</Button>
    {!busy && !failed ? <p className="auth-alt-action"><Link className="link" href="/innovation">取消，返回创新创业</Link></p> : null}
  </>;
}
