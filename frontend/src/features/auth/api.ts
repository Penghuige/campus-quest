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

const BASE = "/api/v1/auth";

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

/** Student login; the backend sets the refresh + CSRF cookies. */
export function loginStudent(
  username: string,
  password: string,
): Promise<LoginTokens> {
  return apiRequest<LoginTokens>(`${BASE}/login`, {
    method: "POST",
    body: { username, password },
  });
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
