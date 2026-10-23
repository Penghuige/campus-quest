/**
 * The unified challenge family's wire pins (unified-login spec §3;
 * contract-first against the frozen field-name consensus with the
 * backend's PR-1): ONE identifier (E.164 phone or email) drives the
 * code stream, the verify answer is a THREE-arm status-discriminated
 * union, and the password login rides the SAME union on /auth/login.
 *
 * Same harness decision as the family's other pin files: stubbed
 * globalThis.fetch, every request recorded. Pins:
 * - paths + exact body members (`identifier`, `code`, `pending_token`,
 *   `totp_code`, `password`, `remember_device` — presence-based);
 * - the three verify arms: authenticated (tokens ADOPTED, role
 *   surfaced), totp_required (NOTHING adopted), unregistered (nothing
 *   adopted — the register leg owns the login);
 * - remember_device rides verify/totp/login ONLY when checked — the
 *   backend routes its semantics by audience (the 2026-10-15 terminal
 *   ruling enforced server-side; the client has no pre-verify audience
 *   knowledge to gate on).
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  fastLoginCookieOnly,
  fetchRememberedHint,
  loginWithPassword,
  requestUnifiedChallenge,
  unifiedChallengeTotp,
  verifyUnifiedChallenge,
} from "../features/auth/api";
import { matchRememberedSlot } from "../features/auth/unifiedLoginView";
import { clearAccessToken, getAccessToken } from "../lib/accessToken";

type RecordedRequest = {
  url: string;
  method: string;
  headers: Headers;
  credentials: RequestCredentials | undefined;
  body: string | null;
};

let recorded: RecordedRequest | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status: number, headers: Record<string, string> = {}) {
  // 202 with an empty body is legal; undici only rejects explicit
  // bodies on 204/205.
  responseFor = () => new Response(body.length > 0 ? body : null, { status, headers });
}

async function rejectionOf(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(
    () => assert.fail("expected rejection"),
    (error: unknown) => error,
  );
}

beforeEach(() => {
  recorded = undefined;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    recorded = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
      headers: new Headers(init?.headers),
      credentials: init?.credentials,
      body: typeof init?.body === "string" ? init.body : null,
    };
    return responseFor();
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("requestUnifiedChallenge", () => {
  test("POSTs {identifier} to the unified request; the uniform 202 resolves void", async () => {
    stubFetch("{}", 202);
    await requestUnifiedChallenge("teacher@school.edu");
    assert.equal(recorded?.url, "/api/v1/auth/challenge/request");
    assert.equal(recorded?.method, "POST");
    assert.equal(recorded?.credentials, "include");
    assert.equal(recorded?.headers.get("Content-Type"), "application/json");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      identifier: "teacher@school.edu",
    });
  });
});

describe("verifyUnifiedChallenge", () => {
  test("the authenticated arm ADOPTS the nested tokens and surfaces the role", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({
        status: "authenticated",
        role: "TEACHER",
        tokens: { access_token: "t-at", csrf_token: "ct", token_type: "bearer" },
      }),
      200,
    );
    const outcome = await verifyUnifiedChallenge("teacher@school.edu", "234567", false);
    assert.equal(recorded?.url, "/api/v1/auth/challenge/verify");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      identifier: "teacher@school.edu",
      code: "234567",
    });
    assert.deepEqual(outcome, { kind: "authenticated", role: "TEACHER" });
    assert.equal(getAccessToken(), "t-at");
  });

  test("remember_device rides the body ONLY when checked (presence-based)", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({
        status: "authenticated",
        role: "STUDENT",
        tokens: { access_token: "s-at", csrf_token: "ct", token_type: "bearer" },
      }),
      200,
    );
    await verifyUnifiedChallenge("13900001234", "234567", true);
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      identifier: "13900001234",
      code: "234567",
      remember_device: true,
    });
  });

  test("the totp_required arm adopts NOTHING and hands the pending token", async () => {
    clearAccessToken();
    stubFetch(JSON.stringify({ status: "totp_required", pending_token: "p-at" }), 200);
    const outcome = await verifyUnifiedChallenge("admin@school.edu", "234567", false);
    assert.deepEqual(outcome, { kind: "totp_required", pendingToken: "p-at" });
    assert.equal(getAccessToken(), null);
  });

  test("the unregistered arm adopts NOTHING and hands the register token", async () => {
    clearAccessToken();
    stubFetch(JSON.stringify({ status: "unregistered", register_token: "r-tok" }), 200);
    const outcome = await verifyUnifiedChallenge("13900009999", "234567", false);
    assert.deepEqual(outcome, { kind: "unregistered", registerToken: "r-tok" });
    assert.equal(getAccessToken(), null);
  });

  test("a body without a known status member throws — the contract admits exactly three arms", async () => {
    clearAccessToken();
    stubFetch(JSON.stringify({ access_token: "t-at", csrf_token: "ct" }), 200);
    const error = await rejectionOf(verifyUnifiedChallenge("teacher@school.edu", "234567", false));
    assert.match(String(error), /unrecognized/i);
    assert.equal(getAccessToken(), null);
  });
});

describe("unifiedChallengeTotp", () => {
  test("posts {pending_token, totp_code} (totp_code, NOT the old family's code) and adopts", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({
        status: "authenticated",
        role: "ADMIN",
        tokens: { access_token: "a-at", csrf_token: "ct", token_type: "bearer" },
      }),
      200,
    );
    const outcome = await unifiedChallengeTotp("p-at", "234567", false);
    assert.equal(recorded?.url, "/api/v1/auth/challenge/totp");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      pending_token: "p-at",
      totp_code: "234567",
    });
    assert.deepEqual(outcome, { kind: "authenticated", role: "ADMIN" });
    assert.equal(getAccessToken(), "a-at");
  });

  test("remember_device rides ONLY the totp leg when checked — the checkbox's meaning lives here", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({
        status: "authenticated",
        role: "ADMIN",
        tokens: { access_token: "a-at", csrf_token: "ct", token_type: "bearer" },
      }),
      200,
    );
    await unifiedChallengeTotp("p-at", "234567", true);
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      pending_token: "p-at",
      totp_code: "234567",
      remember_device: true,
    });
  });
});

describe("the remembered-slot layer-0 pair (the multi-slot batch, 2026-10-24)", () => {
  test("the probe answers the slot ARRAY — mask + handle, order preserved", async () => {
    stubFetch(
      JSON.stringify({
        remembered: [
          { identifier_masked: "t***@school.edu", slot: "a1b2c3d4e5" },
          { identifier_masked: "+86****5555", slot: "f6g7h8i9j0" },
        ],
      }),
      200,
    );
    const slots = await fetchRememberedHint();
    assert.deepEqual(slots, [
      { identifier_masked: "t***@school.edu", slot: "a1b2c3d4e5" },
      { identifier_masked: "+86****5555", slot: "f6g7h8i9j0" },
    ]);
    assert.equal(recorded?.url, "/api/v1/auth/challenge/remembered");
    assert.equal(recorded?.method, "GET");
    assert.equal(recorded?.credentials, "include");
  });

  test("dirty rows filter out; a non-array body degrades to empty — never throws", async () => {
    stubFetch(
      JSON.stringify({
        remembered: [
          { identifier_masked: "t***@school.edu", slot: "a1b2c3d4e5" },
          { identifier_masked: "missing-slot" },
          { slot: "missing-mask" },
          "garbage",
        ],
      }),
      200,
    );
    assert.deepEqual(await fetchRememberedHint(), [
      { identifier_masked: "t***@school.edu", slot: "a1b2c3d4e5" },
    ]);
    stubFetch(JSON.stringify({ remembered: "nope" }), 200);
    assert.deepEqual(await fetchRememberedHint(), []);
  });

  test("the probe answers EMPTY for the uniform 204 — and NEVER throws (any failure is empty)", async () => {
    stubFetch("", 204);
    assert.deepEqual(await fetchRememberedHint(), []);
    // A surprise status also resolves empty quietly — the probe decides
    // a banner's existence, nothing more.
    responseFor = () => {
      throw new Error("transport");
    };
    assert.deepEqual(await fetchRememberedHint(), []);
  });

  test("the one-click posts an EMPTY body verify — no target takes the most recent slot", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({
        status: "authenticated",
        role: "STUDENT",
        tokens: { access_token: "s-at", csrf_token: "ct", token_type: "bearer" },
      }),
      200,
    );
    const outcome = await fastLoginCookieOnly();
    assert.equal(recorded?.url, "/api/v1/auth/challenge/verify");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {});
    assert.deepEqual(outcome, { kind: "authenticated", role: "STUDENT" });
    assert.equal(getAccessToken(), "s-at");
  });

  test("a TARGET ride names the matched slot handle — the popover pick's account", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({
        status: "authenticated",
        role: "TEACHER",
        tokens: { access_token: "t-at", csrf_token: "ct", token_type: "bearer" },
      }),
      200,
    );
    const outcome = await fastLoginCookieOnly("f6g7h8i9j0");
    assert.equal(recorded?.url, "/api/v1/auth/challenge/verify");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), { target: "f6g7h8i9j0" });
    assert.deepEqual(outcome, { kind: "authenticated", role: "TEACHER" });
    assert.equal(getAccessToken(), "t-at");
  });

  test("a remembered ADMIN's one-click answers totp_required — the exemption never resets", async () => {
    clearAccessToken();
    stubFetch(JSON.stringify({ status: "totp_required", pending_token: "p-at" }), 200);
    const outcome = await fastLoginCookieOnly();
    assert.deepEqual(outcome, { kind: "totp_required", pendingToken: "p-at" });
    assert.equal(getAccessToken(), null);
  });
});

describe("matchRememberedSlot (the client matching contract — mask ∈ array, echo the slot)", () => {
  test("a mask present in the probe array answers THAT entry's slot", async () => {
    const slots = await Promise.resolve([
      { identifier_masked: "t***@school.edu", slot: "a1b2c3d4e5" },
      { identifier_masked: "+86****5555", slot: "f6g7h8i9j0" },
    ]);
    assert.equal(matchRememberedSlot("+86****5555", slots), "f6g7h8i9j0");
    assert.equal(matchRememberedSlot("t***@school.edu", slots), "a1b2c3d4e5");
  });

  test("a mask matching nothing answers null — the code flow's turn (credential gone unifies with unmatched)", async () => {
    const slots = await Promise.resolve([
      { identifier_masked: "t***@school.edu", slot: "a1b2c3d4e5" },
    ]);
    assert.equal(matchRememberedSlot("+86****9999", slots), null);
    assert.equal(matchRememberedSlot("+86****9999", []), null);
  });
});

describe("loginWithPassword (the /auth/login identifier extension)", () => {
  test("posts {identifier, password} — the student number is a legal password identifier", async () => {
    clearAccessToken();
    stubFetch(
      JSON.stringify({
        status: "authenticated",
        role: "STUDENT",
        tokens: { access_token: "s-at", csrf_token: "ct", token_type: "bearer" },
      }),
      200,
    );
    const outcome = await loginWithPassword("20250101", "correct-horse", false);
    assert.equal(recorded?.url, "/api/v1/auth/login");
    assert.equal(recorded?.method, "POST");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      identifier: "20250101",
      password: "correct-horse",
    });
    assert.deepEqual(outcome, { kind: "authenticated", role: "STUDENT" });
    assert.equal(getAccessToken(), "s-at");
  });

  test("the ADMIN password arm answers totp_required — the second leg rides challenge/totp", async () => {
    clearAccessToken();
    stubFetch(JSON.stringify({ status: "totp_required", pending_token: "p-at" }), 200);
    const outcome = await loginWithPassword("admin@school.edu", "correct-horse", true);
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      identifier: "admin@school.edu",
      password: "correct-horse",
      remember_device: true,
    });
    assert.deepEqual(outcome, { kind: "totp_required", pendingToken: "p-at" });
    assert.equal(getAccessToken(), null);
  });
});
