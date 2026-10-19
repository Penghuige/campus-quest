"use client";
/**
 * The staff login page's TWO channel tabs (the convergence batch,
 * 2026-10-20): the UNIFIED code channel (one identifier field —
 * 邮箱或手机号) is the DEFAULT; the password+TOTP form (the pre-V1.1
 * channel, still first-class for accounts that set a password — spec
 * §2.4's compatibility clause) sits one visible toggle away. The
 * structure rides the SAME ChannelToggle the student page does — the
 * two login pages differ only in labels and channels now.
 */
import { useState, type ReactNode } from "react";

import { ChannelToggle } from "./ChannelToggle";

type Channel = "code" | "password";

const CHANNEL_LABELS: Record<Channel, string> = {
  code: "验证码登录",
  password: "密码登录",
};

export function StaffLoginPanel({
  codeChannel,
  passwordChannel,
}: {
  codeChannel: ReactNode;
  passwordChannel: ReactNode;
}) {
  const [channel, setChannel] = useState<Channel>("code");

  return (
    <>
      <ChannelToggle
        options={[
          { key: "code", label: CHANNEL_LABELS.code },
          { key: "password", label: CHANNEL_LABELS.password },
        ]}
        active={channel}
        onSelect={(key) => setChannel(key as Channel)}
      />
      {channel === "code" ? codeChannel : passwordChannel}
    </>
  );
}
