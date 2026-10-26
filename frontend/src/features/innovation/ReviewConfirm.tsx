"use client";
import { Button } from "@/components/ui/button";
import { useId, useRef } from "react";
import { getAuthEpoch } from "@/lib/accessToken";
import { Dialog, DialogContent, DialogFooter, DialogTitle } from "@/components/ui/dialog";
import { restoreReviewFocus } from "./reviewFocus";

export function ReviewConfirm({ title, description, open, busy, onClose, onConfirm, fallbackFocus }: { title: string; description: string; open: boolean; busy: boolean; onClose: () => void; onConfirm: () => void; fallbackFocus?: () => HTMLElement | null }) {
  const titleId = useId(); const hintId = useId();
  const opener = useRef<{ element: HTMLElement; epoch: number } | null>(null);
  return <Dialog open={open} onOpenChange={(value) => { if (!value && !busy) onClose(); }}><DialogContent aria-labelledby={titleId} aria-describedby={hintId} onOpenAutoFocus={() => {
    opener.current = document.activeElement instanceof HTMLElement ? { element: document.activeElement, epoch: getAuthEpoch() } : null;
  }} onCloseAutoFocus={(event) => {
    event.preventDefault();
    restoreReviewFocus(opener.current, fallbackFocus?.() ?? null, getAuthEpoch());
  }}>
    <DialogTitle id={titleId}>{title}</DialogTitle><p className="field-hint" id={hintId}>{description}</p>
    <DialogFooter><Button variant="secondary" disabled={busy} onClick={onClose}>取消</Button><Button disabled={busy} onClick={onConfirm}>{busy ? "正在处理…" : "确认"}</Button></DialogFooter>
  </DialogContent></Dialog>;
}
