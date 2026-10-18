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

import { adoptFullLogin } from "./api";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];

/** The pending-approval row: the generated admin listing shape — its
 * phone_masked is the project-uniform mask (+861****5555, backend
 * mask_phone; G11 minimal necessity) which the approvals page renders
 * verbatim (the client never re-masks). */
export type PendingStudentDto = Schemas["AdminUserResponse"];

/** Ask for a 6-digit SMS login code. 202 always (anti-enumeration). */
export function requestMobileLoginCode(phone: string): Promise<void> {
  return apiRequest<void>("/api/v1/auth/mobile-code/request", {
    method: "POST",
    body: { phone },
  });
}

/**
 * The verify outcome (the send-code-as-entry batch, 2026-10-19): a
 * REGISTERED phone's code completes the login (tokens, adopted); an
 * UNREGISTERED phone's code answers `{status:"unregistered",
 * register_token}` — the login page's INLINE third step (学号 +
 * optional password) consumes it via registerViaMobileCode. No token
 * is adopted on the unregistered arm (the register leg owns that).
 */
export type MobileVerifyOutcome =
  | { kind: "tokens"; tokens: Schemas["TokenPairResponse"] }
  | { kind: "unregistered"; registerToken: string };

/** Trade the SMS code for a login — or the unregistered register leg. */
export async function verifyMobileLoginCode(
  phone: string,
  code: string,
  rememberDevice: boolean,
): Promise<MobileVerifyOutcome> {
  const body = await apiRequest<unknown>("/api/v1/auth/mobile-code/verify", {
    method: "POST",
    body: rememberDevice
      ? { phone, code, remember_device: true }
      : { phone, code },
  });
  const record = body as { status?: unknown; register_token?: unknown };
  if (
    typeof record === "object" &&
    record !== null &&
    record.status === "unregistered" &&
    typeof record.register_token === "string"
  ) {
    return { kind: "unregistered", registerToken: record.register_token };
  }
  const tokens = body as Schemas["TokenPairResponse"];
  adoptFullLogin(tokens);
  return { kind: "tokens", tokens };
}

/**
 * The inline register leg (POST /auth/mobile-code/register): the
 * unregistered arm's register_token + the student number (and the
 * register page's OPTIONAL-password semantics — null when unset)
 * complete the account AND the login; the tokens adopt the shared
 * dance and the caller lands on the student home (PENDING banner
 * included — every fresh registration is PENDING by §1).
 */
export async function registerViaMobileCode(
  registerToken: string,
  studentNumber: string,
  password: string | null,
): Promise<{ kind: "tokens"; tokens: Schemas["TokenPairResponse"] }> {
  const tokens = await apiRequest<Schemas["TokenPairResponse"]>(
    "/api/v1/auth/mobile-code/register",
    {
      method: "POST",
      body: {
        register_token: registerToken,
        student_number: studentNumber,
        password,
      },
    },
  );
  adoptFullLogin(tokens);
  return { kind: "tokens", tokens };
}

/**
 * The remembered-device HINT probe (owner feedback ③, 2026-10-17;
 * frozen contract): GET /auth/mobile-code/remembered answers
 * 200 {phone_masked} when the HttpOnly cq_staff_device cookie names a
 * live remembered STUDENT device (the mask is the backend's
 * mask_phone — the client never sees the full number), and a uniform
 * 204 otherwise. Every non-answer — 204, a surprise status, transport
 * failure — resolves `null` quietly: the probe decides whether the
 * login page renders its one-click banner and must never disturb the
 * code form (无 cookie 时登录页零多余元素).
 */
export async function fetchRememberedHint(): Promise<string | null> {
  try {
    const body = await apiRequest<{ phone_masked?: string } | null>(
      "/api/v1/auth/mobile-code/remembered",
    );
    return body?.phone_masked ?? null;
  } catch {
    return null;
  }
}

/**
 * The banner's CLICK leg (owner feedback ③): the login page holds only
 * the probe's MASK, never the full phone — so the HttpOnly
 * cq_staff_device cookie alone locates the user (it is already a full
 * single-factor credential; the trust level is unchanged) and the
 * verify body is EMPTY. The returned login adopts the shared dance;
 * a refusal (cookie raced/expired between probe and click) rejects and
 * the banner falls back to the code form's copy.
 */
export async function fastLoginCookieOnly(): Promise<Schemas["TokenPairResponse"]> {
  const tokens = await apiRequest<Schemas["TokenPairResponse"]>(
    "/api/v1/auth/mobile-code/verify",
    { method: "POST", body: {} },
  );
  adoptFullLogin(tokens);
  return tokens;
}

/** The pending-approval queue (the admin users list, status-filtered). */
export function listPendingStudents(): Promise<{ items: PendingStudentDto[] }> {
  return apiRequest<{ items: PendingStudentDto[] }>(
    "/api/v1/admin/users?status=PENDING_APPROVAL",
  );
}

/** Approve one student (audit USER_APPROVED server-side). */
export function approveStudent(userId: string): Promise<PendingStudentDto> {
  return apiRequest<PendingStudentDto>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/approve`,
    { method: "POST" },
  );
}

/** Reject one student (optional reason rides the audit row). */
export function rejectStudent(
  userId: string,
  reason: string | null,
): Promise<PendingStudentDto> {
  return apiRequest<PendingStudentDto>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/reject`,
    { method: "POST", body: { reason } },
  );
}
