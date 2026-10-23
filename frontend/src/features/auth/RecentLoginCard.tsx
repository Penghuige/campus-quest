"use client";
/**
 * The recent-login card's pick face (batch 2 → popover batch,
 * 2026-10-23): ONE shared renderer for both seats — the login page's
 * quick-switch cards and the shells' 切换账号 menu. The G11 discipline
 * lives here once: the card shows the masked form only; the full
 * identifier never renders, it only rides the pick callback (the
 * field the user is about to use).
 *
 * The avatar slot is a role-tinted initial (the anonymous surfaces
 * cannot fetch avatars — avatarUrl needs a signed-in bearer; noted as
 * batch 2's one deviation from the brief).
 */
import type { RecentLoginEntry } from "./recentLogins";

const ROLE_BADGE: Record<string, string> = {
  STUDENT: "学生",
  TEACHER: "教师",
  ADMIN: "管理员",
};

export function RecentLoginCardButton({
  entry,
  onPick,
}: {
  entry: RecentLoginEntry;
  onPick: (entry: RecentLoginEntry) => void;
}) {
  return (
    <button
      type="button"
      className="login-recent-pick"
      onClick={() => onPick(entry)}
      aria-label={`切换到 ${entry.masked}`}
    >
      <span
        className={`login-recent-avatar role-${entry.role.toLowerCase()}`}
        aria-hidden="true"
      >
        {(entry.nickname || entry.masked)[0]}
      </span>
      <span className="login-recent-meta">
        <span className="mono">{entry.masked}</span>
        <span className="login-recent-sub">
          {ROLE_BADGE[entry.role] ?? entry.role}
          {entry.nickname ? ` · ${entry.nickname}` : ""}
        </span>
      </span>
    </button>
  );
}
