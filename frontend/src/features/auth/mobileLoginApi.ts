/**
 * Student approvals API wrappers (auth-extension-II spec §5) — the
 * register leg + the admin approval queue. The mobile-code LOGIN
 * family (request/verify) and the remembered-device pair retired with
 * the unified-login batch: the login surfaces ride the challenge
 * family in api.ts (spec §3), and the register leg keeps ITS frozen
 * path and envelope (§3.3: not in the retirement list).
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

/**
 * The inline register leg (POST /auth/mobile-code/register — §3.3:
 * untouched): the unregistered arm's register_token (now handed by
 * /auth/challenge/verify) + the student number (and the register
 * page's OPTIONAL-password semantics — null when unset; nickname
 * rides the backend's student-number default) complete the account
 * AND the login. The 201 body is the ④ envelope `{user, tokens}` —
 * the tokens adopt the shared dance and the caller lands on the
 * student home (PENDING banner included — every fresh registration is
 * PENDING by §1).
 */
export async function registerViaMobileCode(
  registerToken: string,
  studentNumber: string,
  password: string | null,
): Promise<{ kind: "tokens"; tokens: Schemas["TokenPairResponse"] }> {
  const body = (await apiRequest<{ user?: unknown; tokens?: Schemas["TokenPairResponse"] }>(
    "/api/v1/auth/mobile-code/register",
    {
      method: "POST",
      body: {
        register_token: registerToken,
        student_number: studentNumber,
        password,
      },
    },
  )) as { user?: unknown; tokens?: Schemas["TokenPairResponse"] };
  if (typeof body !== "object" || body === null || body.tokens === undefined) {
    throw new Error("mobile-code register: the 201 body is not the {user, tokens} envelope");
  }
  adoptFullLogin(body.tokens);
  return { kind: "tokens", tokens: body.tokens };
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
