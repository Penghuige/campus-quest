"use client";
/**
 * Small clipboard-copy button for one-time secrets (T8 staff 2FA setup).
 *
 * `navigator.clipboard.writeText` with graceful degradation: a rejected
 * clipboard permission (or a non-secure context) flips the SAME button
 * into a 已复制/失败 status instead of throwing — the user always keeps
 * the manual select-and-copy path because the value stays rendered as
 * text next to the button (§12: never color/status alone — the label
 * itself changes, announced via `aria-live`).
 *
 * Nothing is logged: the value transits only to the clipboard API.
 */
import { useEffect, useRef, useState } from "react";

export interface CopyButtonProps {
  /** The exact text to place on the clipboard. */
  value: string;
  /** Stable accessible label naming WHAT gets copied (e.g. 复制密钥). */
  label: string;
}

const RESET_MS = 2500;

export function CopyButton({ value, label }: CopyButtonProps) {
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current !== null) {
        clearTimeout(timer.current);
      }
    };
  }, []);

  async function onCopy() {
    try {
      await navigator.clipboard.writeText(value);
      setStatus("copied");
    } catch {
      setStatus("failed");
    }
    if (timer.current !== null) {
      clearTimeout(timer.current);
    }
    timer.current = setTimeout(() => setStatus("idle"), RESET_MS);
  }

  return (
    <span className="copy-control">
      <button type="button" className="btn btn-ghost" onClick={() => void onCopy()}>
        {label}
      </button>
      <span className="copy-status" aria-live="polite">
        {status === "copied" ? "已复制" : status === "failed" ? "复制失败，请手动选择复制" : ""}
      </span>
    </span>
  );
}
