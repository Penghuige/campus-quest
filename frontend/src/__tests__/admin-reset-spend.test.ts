/**
 * The §3.2 admin-issued reset-token spend leg (Wave 2a terminal
 * round): PUBLIC — the one-time urlsafe32 token IS the authorization
 * (its SHA-256 lives server-side, 48h TTL, GETDEL at spend — a
 * replay answers the uniform invalid). Pins:
 * - POST /auth/password/reset-token {reset_token, new_password} → 204
 *   void (every session revoked server-side; the user logs in fresh);
 * - cookie credentials (the route is public, but the call stays
 *   uniform with the family).
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import { spendAdminResetToken } from "../features/auth/api";

let recorded:
  | {
      url: string;
      method: string;
      credentials: RequestCredentials | undefined;
      body: string | null;
    }
  | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

beforeEach(() => {
  recorded = undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    recorded = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
      credentials: init?.credentials,
      body: typeof init?.body === "string" ? init.body : null,
    };
    return responseFor();
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("spendAdminResetToken (the public §3.2 consume leg)", () => {
  test("posts {reset_token, new_password}; the 204 resolves void", async () => {
    responseFor = () => new Response(null, { status: 204 });
    await spendAdminResetToken("urlsafe32-token", "e2e-correct-horse");
    assert.equal(recorded?.url, "/api/v1/auth/password/reset-token");
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.credentials, "include");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      reset_token: "urlsafe32-token",
      new_password: "e2e-correct-horse",
    });
  });
});
