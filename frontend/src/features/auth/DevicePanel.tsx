"use client";
/**
 * Own remembered-devices panel (V1.1 §2.2; plan Task 11 frontend half)
 * — list the 30-day remember-device credentials on the caller's
 * account and revoke them all in one confirmed action.
 *
 * Mount note (task ruling): mounted on the STAFF profile view — the
 * cq_staff_device credential is staff-only (an ADMIN's TOTP bypass), so
 * the student AccountSettings would carry a permanently-empty section.
 * The row shape is the frozen contract; token hashes never leave the
 * server (G11 — nothing secret is displayable here by construction).
 */
import { useEffect, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import { SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";

import { Button } from "@/components/ui/button";
import { listMyDevices, revokeMyDevices, type MyDeviceDto } from "./api";
import { COPY_GENERIC_FAIL } from "@/lib/errors";

function deviceTime(value: string | null): string {
  return value === null ? "—" : formatDeadlineDateTime(parseServerInstant(value));
}

export function DevicePanel() {
  const [devices, setDevices] = useState<MyDeviceDto[]>([]);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [reloadSeed, setReloadSeed] = useState(0);
  const [revoking, setRevoking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    listMyDevices().then(
      (rows) => {
        if (!cancelled) {
          setDevices(rows);
          setPhase("ready");
        }
      },
      () => {
        if (!cancelled) {
          setPhase("error");
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [reloadSeed]);

  /** The lint-driven state shape: the loading phase is set by the
   * TRIGGER (initial mount state / the refresh buttons), never inside
   * the effect body. */
  function reload() {
    setPhase("loading");
    setNote(null);
    setReloadSeed((seed) => seed + 1);
  }

  async function onRevokeAll() {
    if (busy) {
      return;
    }
    setBusy(true);
    setActionError(null);
    try {
      const result = await revokeMyDevices();
      setRevoking(false);
      setNote(`已移除 ${result.revoked} 台记住的设备；下次登录需重新验证动态口令`);
      setReloadSeed((seed) => seed + 1);
    } catch (cause) {
      setActionError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="section" aria-label="登录设备">
      <div className="section-head">
        <h2 className="section-title">登录设备</h2>
        <Button variant="ghost" onClick={reload}>
          刷新
        </Button>
      </div>
      <p className="field-hint">
        登录时勾选「记住此设备 30 天」会生成一台设备凭据；全部移除后，下次登录需重新完成动态口令验证。
      </p>
      {phase === "loading" ? (
        <SectionSkeleton lines={2} />
      ) : phase === "error" ? (
        <SectionError error={null} onRetry={reload} retryLabel="重新加载" />
      ) : devices.length === 0 ? (
        <p className="field-hint">暂无记住的设备。</p>
      ) : (
        <div className="table-scroll">
          <table className="staff-table" aria-label="记住的登录设备">
            <thead>
              <tr>
                <th scope="col">添加时间</th>
                <th scope="col">最近使用</th>
                <th scope="col">有效期至</th>
              </tr>
            </thead>
            <tbody>
              {devices.map((device) => (
                <tr key={device.id}>
                  <td>
                    <time dateTime={device.created_at}>{deviceTime(device.created_at)}</time>
                  </td>
                  <td>
                    {device.last_used_at !== null ? (
                      <time dateTime={device.last_used_at}>
                        {deviceTime(device.last_used_at)}
                      </time>
                    ) : (
                      deviceTime(null)
                    )}
                  </td>
                  <td>
                    <time dateTime={device.expires_at}>{deviceTime(device.expires_at)}</time>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {note !== null ? (
        <p className="alert alert-success" role="status">
          {note}
        </p>
      ) : null}
      {actionError !== null ? (
        <div className="alert alert-error" role="alert">
          <p>
            <span className="alert-marker" aria-hidden="true">!</span>
            {COPY_GENERIC_FAIL}
          </p>
        </div>
      ) : null}
      {phase === "ready" && devices.length > 0 ? (
        <div className="dialog-actions">
          <Button variant="danger" onClick={() => setRevoking(true)} disabled={busy}>
            全部移除
          </Button>
        </div>
      ) : null}
      {revoking ? (
        <Dialog
          open
          onOpenChange={(next) => {
            if (!next && !busy) {
              setRevoking(false);
            }
          }}
        >
          <DialogContent
            asChild
            aria-labelledby="devices-revoke-title"
            onPointerDownOutside={(event) => {
              // Deliberate: no backdrop-click close.
              event.preventDefault();
            }}
          >
            <div>
              <DialogTitle id="devices-revoke-title">移除全部记住的设备</DialogTitle>
              <p className="report-target">
                将移除当前账号全部 {devices.length} 台记住的设备；移除后下次登录需重新完成动态口令验证。
              </p>
              <DialogFooter>
                <Button variant="danger" onClick={() => void onRevokeAll()} disabled={busy} aria-busy={busy}>
                  {busy ? <span className="spinner" aria-hidden="true" /> : null}
                  <span>确认移除</span>
                </Button>
                <Button variant="secondary" onClick={() => setRevoking(false)} disabled={busy}>
                  取消
                </Button>
              </DialogFooter>
            </div>
          </DialogContent>
        </Dialog>
      ) : null}
    </section>
  );
}
