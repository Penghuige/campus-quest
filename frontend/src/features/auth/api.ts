/**
 * Typed wrappers for the identity auth endpoints (spec §5; backend
 * identity/auth_router.py). Shapes come from the GENERATED OpenAPI types in
 * `lib/api/schema` — the field names below are the backend contract:
 * `student_number`, `nickname`, `phone_token`, `password`, `username`,
 * `challenge_id`, `code`, `new_password`.
 *
 * All calls go through `apiRequest` (cookies, CSRF double-submit, §29
 * envelope errors as `ApiError`); refresh-token handling stays entirely in
 * the HttpOnly cookie the backend sets.
 */
import { apiRequest } from "../../lib/api";
import {
  beginAuthTransition,
  endAuthTransition,
  recordLogin,
  recordLogout,
} from "../../lib/accessToken";
import { invalidateSessionCache } from "./session";
import type { components } from "../../lib/api/schema";

type Schemas = components["schemas"];

/** `ChallengeResponse` — challenge id + expiry; never the code (§33.2). */
export type ChallengeView = Schemas["ChallengeResponse"];

/** `PhoneTokenResponse` — the single-use REGISTER proof. */
export type PhoneTokenView = Schemas["PhoneTokenResponse"];

/** `RegisterRequest` body. */
export type RegisterPayload = Schemas["RegisterRequest"];

/** `UserPublic` — the created account view. */
export type RegisteredUser = Schemas["UserPublic"];

/** `PasswordResetRequest` body. */
export type PasswordResetPayload = Schemas["PasswordResetRequest"];

/** `TokenPairResponse` — short-lived access token; refresh rides the cookie. */
export type LoginTokens = Schemas["TokenPairResponse"];

/** `MePublic` — the owner's own account page (spec §40: contacts included). */
export type MeDto = Schemas["MePublic"];

/** `EmailChallengeResponse` — request-side view of one email cycle. */
export type EmailChallengeDto = Schemas["EmailChallengeResponse"];

const BASE = "/api/v1/auth";
const ME = "/api/v1/me";

/** Issue a REGISTER-purpose phone OTP challenge (§5.4, §33.2). */
export function requestPhoneChallenge(phone: string): Promise<ChallengeView> {
  return apiRequest<ChallengeView>(`${BASE}/phone/challenges`, {
    method: "POST",
    body: { phone },
  });
}

/** Trade the SMS code for the single-use phone token. */
export function verifyPhoneChallenge(
  challengeId: string,
  code: string,
): Promise<PhoneTokenView> {
  return apiRequest<PhoneTokenView>(
    `${BASE}/phone/challenges/${encodeURIComponent(challengeId)}/verify`,
    { method: "POST", body: { code } },
  );
}

/** Register one whitelisted student (201 -> `UserPublic`). */
export function registerStudent(payload: RegisterPayload): Promise<RegisteredUser> {
  return apiRequest<RegisteredUser>(`${BASE}/register`, {
    method: "POST",
    body: payload,
  });
}

/**
 * Student login; the backend sets the refresh + CSRF cookies. The body's
 * short-lived access token is remembered by the memory-only manager, so
 * the next `apiRequest` carries it as the bearer (`lib/accessToken.ts`).
 */
export async function loginStudent(
  username: string,
  password: string,
): Promise<LoginTokens> {
  await beginAuthTransition();
  let tokens: LoginTokens;
  try {
    tokens = await apiRequest<LoginTokens>(`${BASE}/login`, {
      method: "POST",
      body: { username, password },
    });
  } finally {
    endAuthTransition();
  }
  // An explicit login opens a NEW auth context (epoch bump) and
  // synchronously drops any cached ANONYMOUS /me: the login page's
  // hook may have cached it moments ago, and the freshly mounted
  // shell must never render "未登录" from that stale fresh-window
  // entry (targeted re-review P1).
  recordLogin(tokens.access_token);
  invalidateSessionCache();
  return tokens;
}

/**
 * Start password recovery. The response is the IDENTICAL challenge shape for
 * every identifier class (anti-enumeration; profile_service) — the caller
 * must keep its copy uniform too.
 */
export function requestPasswordReset(username: string): Promise<ChallengeView> {
  return apiRequest<ChallengeView>(`${BASE}/password/forgot`, {
    method: "POST",
    body: { username },
  });
}

/** Confirm password recovery (204 on success). */
export function confirmPasswordReset(
  payload: PasswordResetPayload,
): Promise<void> {
  return apiRequest<void>(`${BASE}/password/reset`, {
    method: "POST",
    body: payload,
  });
}

// --- staff surface (spec §5.8; backend identity/staff_router.py) -----------------
//
// Auth model on these four endpoints:
// - `invitations/accept` and `staff/login` behave like the student login:
//   the refresh token rides the HttpOnly cookie; the SHORT-LIVED access
//   token + CSRF mirror sit in the body;
// - `staff/login` success is a full staff session, so its access token
//   enters the memory-only manager exactly like the student login's;
// - the pending staff session's access token is the SANCTIONED body-token
//   exception (backend `PendingStaffSession`): the server confines it to
//   `/staff/totp/*`, so it is deliberately NOT promoted into the shared
//   memory manager (it must never ride unrelated API requests) — the two
//   setup calls below present it as an `Authorization: Bearer` header
//   (the guard reads the bearer scheme — identity/dependencies.py) and
//   the invitation flow keeps it in React state ONLY: never
//   localStorage/sessionStorage, never logged.

/** `TotpSetupResponse` — secret + otpauth URI, displayed exactly once. */
export type StaffTotpSetup = Schemas["TotpSetupResponse"];

/** `TotpConfirmResponse` — plaintext recovery codes, shown exactly once. */
export type StaffTotpConfirm = Schemas["TotpConfirmResponse"];

/** Trade the single-use invitation token for a pending staff session. */
export function acceptStaffInvitation(
  token: string,
  password: string,
): Promise<LoginTokens> {
  return apiRequest<LoginTokens>(`${BASE}/staff/invitations/accept`, {
    method: "POST",
    body: { token, password },
  });
}

/** Staff login: verified email + password + TOTP-or-recovery code. */
export async function loginStaff(
  email: string,
  password: string,
  totpCode: string,
): Promise<LoginTokens> {
  await beginAuthTransition();
  let tokens: LoginTokens;
  try {
    tokens = await apiRequest<LoginTokens>(`${BASE}/staff/login`, {
      method: "POST",
      body: { email, password, totp_code: totpCode },
    });
  } finally {
    endAuthTransition();
  }
  // An explicit login opens a NEW auth context (epoch bump) and
  // synchronously drops any cached ANONYMOUS /me: the login page's
  // hook may have cached it moments ago, and the freshly mounted
  // shell must never render "未登录" from that stale fresh-window
  // entry (targeted re-review P1).
  recordLogin(tokens.access_token);
  invalidateSessionCache();
  return tokens;
}

/**
 * Revoke the presented session server-side (204) and forget the
 * memory-only access token. The memory clears in a `finally` even when
 * the revoke call itself fails: the client must never keep a token the
 * user asked to drop (a survived cookie would simply re-bootstrap on
 * the next request — the server stays the authority).
 */
export async function logout(): Promise<void> {
  await beginAuthTransition();
  try {
    await apiRequest<void>(`${BASE}/logout`, { method: "POST" });
  } finally {
    // An explicit logout CLOSES the auth context (epoch bump): in-
    // flight requests from the closed context must never replay
    // against whoever logs in next (final re-review P0).
    recordLogout();
    // The authenticated /me cache is as stale as the token now.
    invalidateSessionCache();
    endAuthTransition();
  }
}

/** Generate (or rotate) the unconfirmed TOTP secret; shown once (§5.8). */
export function beginTotpSetup(accessToken: string): Promise<StaffTotpSetup> {
  return apiRequest<StaffTotpSetup>("/api/v1/staff/totp/begin", {
    method: "POST",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
}

/** Confirm with one valid code; recovery codes return exactly once. */
export function confirmTotpSetup(
  accessToken: string,
  code: string,
): Promise<StaffTotpConfirm> {
  return apiRequest<StaffTotpConfirm>("/api/v1/staff/totp/confirm", {
    method: "POST",
    headers: { Authorization: `Bearer ${accessToken}` },
    body: { code },
  });
}

// --- own-account profile endpoints (spec §5.4-§5.5, §40; profile_router) --------

/**
 * Change the account nickname (PATCH /me/nickname). No re-auth: nickname
 * is display-only, so the session itself is the authority.
 */
export function updateNickname(nickname: string): Promise<MeDto> {
  return apiRequest<MeDto>(`${ME}/nickname`, {
    method: "PATCH",
    body: { nickname },
  });
}

/**
 * Start a phone change (POST /me/phone/change): re-authenticate with the
 * CURRENT password, then OTP-challenge the NEW phone. The response is
 * the challenge for the new number (spec §5.4).
 */
export function requestPhoneChange(
  password: string,
  newPhone: string,
): Promise<ChallengeView> {
  return apiRequest<ChallengeView>(`${ME}/phone/change`, {
    method: "POST",
    body: { password, new_phone: newPhone },
  });
}

/**
 * Confirm a phone change with the NEW phone's OTP code
 * (POST /me/phone/change/confirm); returns the updated account view.
 */
export function confirmPhoneChange(
  challengeId: string,
  code: string,
): Promise<MeDto> {
  return apiRequest<MeDto>(`${ME}/phone/change/confirm`, {
    method: "POST",
    body: { challenge_id: challengeId, code },
  });
}

/**
 * Bind (or re-bind) an email and send its verification token
 * (POST /me/email); the response carries only the cycle's expiry.
 */
export function requestEmailVerification(email: string): Promise<EmailChallengeDto> {
  return apiRequest<EmailChallengeDto>(`${ME}/email`, {
    method: "POST",
    body: { email },
  });
}

/** Confirm an email binding with the token from the message (§5.5). */
export function confirmEmailVerification(token: string): Promise<MeDto> {
  return apiRequest<MeDto>(`${ME}/email/verify`, {
    method: "POST",
    body: { token },
  });
}

/**
 * Unbind the email after re-authentication (POST /me/email/unbind, 204).
 */
export function unbindEmail(password: string): Promise<void> {
  return apiRequest<void>(`${ME}/email/unbind`, {
    method: "POST",
    body: { password },
  });
}

/**
 * Rotate the password after re-authentication (POST /me/password, 204).
 * Backend semantics (§5.6): every OTHER session dies, this one survives.
 */
export function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  return apiRequest<void>(`${ME}/password`, {
    method: "POST",
    body: { current_password: currentPassword, new_password: newPassword },
  });
}
