"use client";
/**
 * The shared login-channel toggle (the two-channel convergence batch,
 * 2026-10-20): ONE structure both login pages ride — the student page
 * (手机验证码登录 / 密码登录) and the staff page (验证码登录 / 密码登录)
 * differ only in their labels and channels. A real button row with
 * aria-pressed (a tablist would promise arrow-key navigation these
 * lightweight tabs don't implement; the pressed state + visible
 * labels carry the same information accessibly).
 */
import { Button } from "@/components/ui/button";

export interface ChannelOption {
  /** The channel key the owning panel switches on. */
  key: string;
  /** The visible, pressed-state label. */
  label: string;
}

export function ChannelToggle({
  options,
  active,
  onSelect,
  groupLabel = "登录方式",
}: {
  options: readonly ChannelOption[];
  active: string;
  onSelect: (key: string) => void;
  groupLabel?: string;
}) {
  return (
    <div className="login-channel-toggle" role="group" aria-label={groupLabel}>
      {options.map((option) => (
        <Button
          key={option.key}
          variant={option.key === active ? "secondary" : "ghost"}
          aria-pressed={option.key === active}
          onClick={() => onSelect(option.key)}
        >
          {option.label}
        </Button>
      ))}
    </div>
  );
}
