"use client";
import { useId, useRef, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogTitle } from "@/components/ui/dialog";
import { getAuthEpoch } from "@/lib/accessToken";
import { restoreReviewFocus } from "./reviewFocus";

export function RecruitmentConfirm({ title, description, confirmLabel, children, busy, onClose, onConfirm, resultFocus, completed }: { title: string; description: string; confirmLabel: string; children?: ReactNode; busy: boolean; onClose: () => void; onConfirm: () => void; resultFocus: () => HTMLElement | null; completed: boolean }) {
  const titleId = useId(); const hintId = useId();
  const opener = useRef<{ element: HTMLElement; epoch: number } | null>(null);
  return <Dialog open={!completed} onOpenChange={(open) => { if (!open && !busy) onClose(); }}><DialogContent className="ie-recruitment-confirm" aria-labelledby={titleId} aria-describedby={hintId} onOpenAutoFocus={() => {
    opener.current = document.activeElement instanceof HTMLElement ? { element: document.activeElement, epoch: getAuthEpoch() } : null;
  }} onCloseAutoFocus={(event) => {
    event.preventDefault();
    if (completed && opener.current?.epoch === getAuthEpoch()) resultFocus()?.focus();
    else restoreReviewFocus(opener.current, resultFocus(), getAuthEpoch());
  }}>
    <DialogTitle id={titleId}>{title}</DialogTitle><p className="field-hint" id={hintId}>{description}</p>
    {children}
    <DialogFooter><Button variant="secondary" disabled={busy} onClick={onClose}>取消</Button><Button disabled={busy} onClick={onConfirm}>{busy ? "正在处理…" : confirmLabel}</Button></DialogFooter>
  </DialogContent></Dialog>;
}
