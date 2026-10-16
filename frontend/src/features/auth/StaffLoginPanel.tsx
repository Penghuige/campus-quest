"use client";
/**
 * The staff login page's channel tabs (V1.1 §2.1; plan Task 8): the
 * email-code flow is the DEFAULT channel; the password+TOTP form (the
 * pre-V1.1 channel, still first-class for accounts that set a
 * password — spec §2.4's compatibility clause) sits one visible toggle
 * away, untouched.
 *
 * The toggle is a real button pair with aria-pressed (a tablist would
 * promise arrow-key navigation these two lightweight tabs don't
 * implement; the pressed state + visible labels carry the same
 * information accessibly).
 */
import { useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";

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
      <div className="login-channel-toggle" role="group" aria-label="登录方式">
        {(["code", "password"] as const).map((option) => (
          <Button
            key={option}
            variant={channel === option ? "secondary" : "ghost"}
            aria-pressed={channel === option}
            onClick={() => setChannel(option)}
          >
            {CHANNEL_LABELS[option]}
          </Button>
        ))}
      </div>
      {channel === "code" ? codeChannel : passwordChannel}
    </>
  );
}
