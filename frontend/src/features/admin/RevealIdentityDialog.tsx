"use client";
/**
 * The explicit anonymous-identity reveal dialog (spec §21.4; plan Task
 * 10 step 2; G11/G12): the ONE surface where a comment author's student
 * number is representable at all. On the plan-14 Dialog primitive
 * (components/ui/dialog; the form is the content element via
 * `DialogContent asChild`, keeping the grid that lived on
 * `form.dialog-body`).
 *
 * Binding rules this component exists to enforce:
 * - the reason is MANDATORY and capped (the backend refuses blanks and
 *   >1000 chars — the dialog mirrors both);
 * - identity renders ONLY after the reveal API SUCCEEDS (the response
 *   is the sole source; nothing about identity is prefetched);
 * - the dialog is mounted ONLY on an explicit per-row action from the
 *   community-moderation context — comment listings never auto-fetch
 *   identities, and the audit row is written server-side on every call.
 */
import { useState, type FormEvent } from "react";

import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import { SectionError } from "@/components/ui/sectionStates";

import { revealCommentIdentity, type RevealIdentityDto } from "./adminApi";
import { adminReasonReady } from "./adminView";

/** Mirrors the backend's RevealRequest cap (raw body, 1000 chars). */
export const REVEAL_REASON_MAX_LENGTH = 1000;

export function RevealIdentityDialog({
  commentId,
  authorDisplay,
  onCancel,
}: {
  commentId: string;
  /** 匿名用户 (or the moderation row's display) — shown pre-reveal only. */
  authorDisplay: string;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  /** Identity exists in state ONLY after a successful reveal call. */
  const [revealed, setRevealed] = useState<RevealIdentityDto | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    if (!adminReasonReady(reason)) {
      setFieldError("追溯原因必填（将记入审计日志）");
      return;
    }
    setFieldError(null);
    setBusy(true);
    setError(null);
    try {
      const identity = await revealCommentIdentity(commentId, reason.trim());
      setRevealed(identity);
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next && !busy) {
          onCancel();
        }
      }}
    >
      <DialogContent
        asChild
        aria-labelledby="reveal-identity-title"
        onPointerDownOutside={(event) => {
          // The legacy dialog had no backdrop-click close.
          event.preventDefault();
        }}
      >
        <form onSubmit={onSubmit} noValidate>
          <DialogTitle id="reveal-identity-title">揭示匿名评论身份</DialogTitle>
          <p className="report-target">
            目标评论作者显示为 {authorDisplay}。揭示身份属于高权限操作，每次调用都会记入审计日志；
            请仅在治理需要时使用，并填写具体追溯原因。
          </p>
          <div className="field">
            <label className="field-label" htmlFor="reveal-reason">
              追溯原因
            </label>
            <textarea
              id="reveal-reason"
              className="input"
              rows={3}
              value={reason}
              onChange={(event) => {
                setReason(event.target.value);
                if (fieldError !== null) {
                  setFieldError(null);
                }
              }}
              aria-invalid={fieldError !== null}
              aria-describedby="reveal-reason-hint"
              maxLength={REVEAL_REASON_MAX_LENGTH}
              disabled={busy || revealed !== null}
              required
            />
            <p className="field-hint" id="reveal-reason-hint">
              必填，不超过 {REVEAL_REASON_MAX_LENGTH} 字符。
            </p>
            {fieldError !== null ? <p className="field-error">{fieldError}</p> : null}
          </div>
          {revealed !== null ? (
            <div className="alert alert-warning" role="status">
              <p>
                <span className="alert-marker" aria-hidden="true">!</span>
                身份已揭示（本次揭示已记入审计日志）：
              </p>
              <p>
                昵称：<strong>{revealed.nickname}</strong>　学号：
                <span className="mono">{revealed.username}</span>
              </p>
              <p className="req-id">用户 ID：{revealed.user_id}</p>
            </div>
          ) : null}
          {error !== null ? <SectionError error={error} /> : null}
          <DialogFooter>
            {revealed === null ? (
              <button
                type="submit"
                className="btn btn-danger"
                disabled={busy}
                aria-busy={busy}
              >
                {busy ? <span className="spinner" aria-hidden="true" /> : null}
                <span>确认揭示身份</span>
              </button>
            ) : null}
            <button
              type="button"
              className="btn btn-secondary"
              onClick={onCancel}
              disabled={busy}
            >
              {revealed !== null ? "关闭" : "取消"}
            </button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
