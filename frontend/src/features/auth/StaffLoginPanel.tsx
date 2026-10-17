"use client";
/**
 * The staff login page's channel tabs (V1.1 §2.1 + login-iteration
 * item ①): the email-code flow stays the DEFAULT channel; the
 * mobile-code flow (the staff account's BOUND phone, item ①) and the
 * password+TOTP form (the pre-V1.1 channel, still first-class for
 * accounts that set a password — spec §2.4's compatibility clause)
 * sit one visible toggle away.
 *
 * The labels disambiguate the two code channels explicitly
 * (邮箱验证码登录 / 手机验证码登录) — item ① added the confusable
 * sibling, so the email tab's bare 验证码登录 copy retired with it.
 *
 * The toggle is a real button row with aria-pressed (a tablist would
 * promise arrow-key navigation these lightweight tabs don't implement;
 * the pressed state + visible labels carry the same information
 * accessibly).
 */
import { useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";

type Channel = "code" | "mobile" | "password";

const CHANNEL_LABELS: Record<Channel, string> = {
  code: "邮箱验证码登录",
  mobile: "手机验证码登录",
  password: "密码登录",
};

export function StaffLoginPanel({
  codeChannel,
  mobileChannel,
  passwordChannel,
}: {
  codeChannel: ReactNode;
  mobileChannel: ReactNode;
  passwordChannel: ReactNode;
}) {
  const [channel, setChannel] = useState<Channel>("code");

  const channels: Record<Channel, ReactNode> = {
    code: codeChannel,
    mobile: mobileChannel,
    password: passwordChannel,
  };

  return (
    <>
      <div className="login-channel-toggle" role="group" aria-label="登录方式">
        {(Object.keys(CHANNEL_LABELS) as Channel[]).map((option) => (
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
      {channels[channel]}
    </>
  );
}
