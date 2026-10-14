"use client";
/**
 * First-login rename nudge (V1.1 §6; plan Task 10): accounts whose
 * nickname still looks machine-generated (all digits, or the username's
 * local part — student numbers and email local parts) get ONE
 * dismissible banner pointing at the rename surface.
 *
 * - shown once per account: dismissal persists in localStorage under
 *   `cq-nudge-rename:{user_id}` (non-secret UI state, the sidebar-
 *   preference pattern); the storage access rides the safeLocalStorage
 *   access guard — a denied storage just means the banner re-appears
 *   next session, never an error;
 * - "never re-shown after rename" is PREDICATE-driven: the me prop
 *   comes from the shell's useSession, so a rename repaints the shell
 *   with a chosen name and the banner stops rendering even with no
 *   dismissal recorded;
 * - the link is role-aware: students own the AccountSettings editor on
 *   /profile; staff land on their profile view (which carries the
 *   nickname edit row this wave added — the read-only shape would have
 *   made the nudge a dead end, see the task ruling).
 */
import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { safeLocalStorage } from "@/lib/sidebarPreference";

import type { MeProfile } from "./session";
import { isAutoNickname, readNudgeDismissed, writeNudgeDismissed } from "./nicknameNudge";

export function NicknameNudge({ me }: { me: MeProfile }) {
  const [dismissed, setDismissed] = useState(() => {
    const storage = safeLocalStorage();
    return storage === null ? false : readNudgeDismissed(storage, me.id);
  });

  if (dismissed || !isAutoNickname(me.nickname, me.username)) {
    return null;
  }

  function onDismiss() {
    const storage = safeLocalStorage();
    if (storage !== null) {
      writeNudgeDismissed(storage, me.id);
    }
    setDismissed(true);
  }

  const renameHref = me.role === "STUDENT" ? "/profile" : "/teacher/profile";

  return (
    <div className="alert alert-warning" role="status">
      <p>
        当前昵称「<span className="mono">{me.nickname}</span>」是系统自动生成的。
        <Link className="link" href={renameHref}>
          把昵称改成好认的名字吧
        </Link>
        ，方便同学和同事认出你。
      </p>
      <p>
        <Button variant="ghost" onClick={onDismiss}>
          暂不修改
        </Button>
      </p>
    </div>
  );
}
