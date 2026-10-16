/**
 * Student mobile-code login + admin approvals API wrappers
 * (auth-extension-II spec §3/§5) — contract-first against the spec
 * text; the backend session's answers may adjust names before its PR
 * lands (then these flip to generated aliases, the PR #72 discipline).
 *
 * Auth model notes:
 * - the mobile channel is the STUDENT surface (staff keep the email
 *   code); request is anti-enumeration 202-always, verify answers a
 *   FULL login (no second factor) — the shared adoption dance applies;
 * - remember_device rides verify (30-day cookie, the #87 craft, student
 *   side included — a remembered phone skips the SMS cost on the next
 *   login);
 * - the approvals endpoints answer the updated user row; the pending
 *   listing rides the admin users list's status filter.
 */
import { apiRequest } from "@/lib/api";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];

export type AdminUserDto = Schemas["AdminUserResponse"];

/** Ask for a 6-digit SMS login code. 202 always (anti-enumeration). */
export function requestMobileLoginCode(phone: string): Promise<void> {
  return apiRequest<void>("/api/v1/auth/mobile-code/request", {
    method: "POST",
    body: { phone },
  });
}

/** Trade the SMS code for a full student login. */
export function verifyMobileLoginCode(
  phone: string,
  code: string,
  rememberDevice: boolean,
): Promise<Schemas["TokenPairResponse"]> {
  return apiRequest<Schemas["TokenPairResponse"]>(
    "/api/v1/auth/mobile-code/verify",
    {
      method: "POST",
      body: rememberDevice
        ? { phone, code, remember_device: true }
        : { phone, code },
    },
  );
}

/** The pending-approval queue (the admin users list, status-filtered). */
export function listPendingStudents(): Promise<{ items: AdminUserDto[] }> {
  return apiRequest<{ items: AdminUserDto[] }>(
    "/api/v1/admin/users?status=PENDING_APPROVAL",
  );
}

/** Approve one student (audit USER_APPROVED server-side). */
export function approveStudent(userId: string): Promise<AdminUserDto> {
  return apiRequest<AdminUserDto>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/approve`,
    { method: "POST" },
  );
}

/** Reject one student (optional reason rides the audit row). */
export function rejectStudent(
  userId: string,
  reason: string | null,
): Promise<AdminUserDto> {
  return apiRequest<AdminUserDto>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/reject`,
    { method: "POST", body: { reason } },
  );
}
