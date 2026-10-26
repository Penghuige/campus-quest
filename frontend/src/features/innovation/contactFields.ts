import type { components } from "@/lib/api/schema";
import { codePointCount } from "./formUtils";

type Schema = components["schemas"];
export type ContactChannel = Schema["ContactInput"]["channel"];

export const CONTACT_LABELS: Record<ContactChannel, string> = { WECHAT: "微信", QQ: "QQ", EMAIL: "邮箱", PHONE: "手机" };

export const RECRUITMENT_CONTACT_INPUTS = {
  WECHAT: { type: "text", inputMode: "text", hint: "6～20 个字符，须以字母开头，可含字母、数字、下划线和减号。", error: "请输入有效的微信号：6～20 个字符，须以字母开头，可含字母、数字、下划线和减号。" },
  QQ: { type: "text", inputMode: "numeric", hint: "5～20 位数字，首位不能为 0。", error: "请输入有效的 QQ 号：5～20 位数字，首位不能为 0。" },
  EMAIL: { type: "email", inputMode: "email", hint: "邮箱格式为名称@域名，例如 qa@example.invalid；最多 254 个字符。", error: "请输入有效的邮箱，格式为名称@域名，例如 qa@example.invalid；最多 254 个字符。" },
  PHONE: { type: "tel", inputMode: "tel", hint: "手机号以数字开头，可在最前加 +；号码部分共 6～25 个字符，可含数字、空格、括号和减号。", error: "请输入有效的手机号：以数字开头，可在最前加 +；号码部分共 6～25 个字符，可含数字、空格、括号和减号。" },
} as const;

export function invalidRecruitmentContactChannels(contacts: { channel: ContactChannel; value: string }[]): ContactChannel[] {
  const seen = new Set<ContactChannel>();
  const invalid = new Set<ContactChannel>();
  for (const { channel, value } of contacts) {
    const trimmed = value.trim();
    const valid = channel === "WECHAT" ? /^[A-Za-z][A-Za-z0-9_-]{5,19}$/.test(trimmed) : channel === "QQ" ? /^[1-9][0-9]{4,19}$/.test(trimmed) : channel === "EMAIL" ? /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed) : /^(?:\+?[0-9][0-9 ()-]{5,24})$/.test(trimmed);
    if (!valid || codePointCount(trimmed) > 254 || seen.has(channel)) invalid.add(channel);
    seen.add(channel);
  }
  return [...invalid];
}
