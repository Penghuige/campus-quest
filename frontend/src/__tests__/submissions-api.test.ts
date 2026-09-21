/**
 * Task 4: submission wire contract against fetch/transport stubs — the
 * intent/complete/validation request shapes (spec §10/§28), the
 * presigned PUT's pinned shape (method, per-type Content-Type, NO auth
 * material — the URL signature is the authorization), and the
 * extension -> declared-type mapping.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  completeUpload,
  createUploadIntent,
  DEFAULT_MAX_UPLOAD_BYTES,
  deriveDeclaredType,
  FILE_PICKER_ACCEPT,
  formatFileSize,
  getValidation,
  putFileToPresignedUrl,
  type PutTransport,
} from "../features/submissions/api";

type RecordedRequest = {
  url: string;
  method: string;
  body: string | null;
};

let recorded: RecordedRequest | undefined;
let responseFor: () => Response;
const originalFetch = globalThis.fetch;

function stubFetch(body: string, status = 200) {
  responseFor = () => new Response(body.length > 0 ? body : null, { status });
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
      body: typeof init?.body === "string" ? init.body : null,
    };
    return responseFor();
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

const CLAIM_ID = "11111111-1111-4111-8111-111111111111";

describe("upload intent wire", () => {
  test("posts claim_id/filename/declared_type/size to the §10 endpoint", async () => {
    stubFetch(
      JSON.stringify({
        intent_id: "22222222-2222-4222-8222-222222222222",
        upload_url: "https://storage.example.local/put-only",
        expires_at: "2026-09-21T09:00:00Z",
      }),
      201,
    );
    const intent = await createUploadIntent(CLAIM_ID, { name: "data.csv", size: 1024 }, "CSV");
    assert.equal(recorded?.url, "/api/v1/submissions/upload-intent");
    assert.equal(recorded?.method, "POST");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      claim_id: CLAIM_ID,
      filename: "data.csv",
      declared_type: "CSV",
      size: 1024,
    });
    assert.equal(intent.intent_id, "22222222-2222-4222-8222-222222222222");
  });
});

describe("upload complete wire", () => {
  test("posts the intent id in the body (the §28 URL-shape ruling)", async () => {
    stubFetch(
      JSON.stringify({
        id: "33333333-3333-4333-8333-333333333333",
        claim_id: CLAIM_ID,
        version: 1,
        original_filename: "data.csv",
        declared_type: "CSV",
        file_size: 1024,
        submitted_at: "2026-09-21T08:00:00Z",
        validation_status: "UPLOADED",
        review_status: "PENDING_REVIEW",
        created_at: "2026-09-21T08:00:00Z",
      }),
      201,
    );
    const submission = await completeUpload("22222222-2222-4222-8222-222222222222");
    assert.equal(recorded?.url, "/api/v1/submissions/upload-complete");
    assert.equal(recorded?.method, "POST");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      intent_id: "22222222-2222-4222-8222-222222222222",
    });
    assert.equal(submission.version, 1);
    assert.equal(submission.validation_status, "UPLOADED");
  });
});

describe("validation fetch wire", () => {
  test("gets the owner's validation view by submission id", async () => {
    stubFetch(
      JSON.stringify({
        submission_id: "33333333-3333-4333-8333-333333333333",
        claim_id: CLAIM_ID,
        version: 1,
        validation_status: "VALIDATING",
        review_status: "PENDING_REVIEW",
        detected_type: null,
        report: null,
      }),
    );
    await getValidation("33333333-3333-4333-8333-333333333333");
    assert.equal(
      recorded?.url,
      "/api/v1/submissions/33333333-3333-4333-8333-333333333333/validation",
    );
    assert.equal(recorded?.method, "GET");
  });
});

describe("presigned PUT transport", () => {
  function fakeTransport(): { transport: PutTransport; calls: Array<Record<string, unknown>> } {
    const calls: Array<Record<string, unknown>> = [];
    return {
      calls,
      transport: {
        async put(url, body, contentType, onProgress, signal) {
          calls.push({ url, body, contentType, signal, onProgress });
          onProgress({ loaded: 5, total: 10 });
        },
      },
    };
  }

  test("PUT carries the file bytes and the PINNED per-type Content-Type", async () => {
    const { transport, calls } = fakeTransport();
    const csv = new Blob(["platform,date\n"], { type: "text/csv" });
    const progress: number[] = [];
    await putFileToPresignedUrl(
      "https://storage.example/signed",
      csv,
      "CSV",
      { onProgress: (sample) => progress.push(sample.total === null ? -1 : sample.loaded / sample.total) },
      transport,
    );
    assert.equal(calls.length, 1);
    assert.equal(calls[0]?.url, "https://storage.example/signed");
    assert.equal(calls[0]?.contentType, "text/csv");
    assert.ok(calls[0]?.body instanceof ArrayBuffer);
    // Progress maps straight through to the caller.
    assert.deepEqual(progress, [0.5]);
  });

  test("the XLSX MIME matches the backend pin exactly", async () => {
    const { transport, calls } = fakeTransport();
    await putFileToPresignedUrl(
      "https://storage.example/signed",
      new Blob(["x"]),
      "XLSX",
      {},
      transport,
    );
    assert.equal(
      calls[0]?.contentType,
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    );
  });

  test("the SQLITE MIME matches the backend pin exactly", async () => {
    const { transport, calls } = fakeTransport();
    await putFileToPresignedUrl(
      "https://storage.example/signed",
      new Blob(["x"]),
      "SQLITE",
      {},
      transport,
    );
    assert.equal(calls[0]?.contentType, "application/vnd.sqlite3");
  });

  test("a transport failure rejects (the flow's upload-failed branch)", async () => {
    const failing: PutTransport = {
      async put() {
        throw new TypeError("network down");
      },
    };
    const error = await rejectionOf(
      putFileToPresignedUrl("https://storage.example/signed", new Blob(["x"]), "CSV", {}, failing),
    );
    assert.ok(error instanceof TypeError);
  });
});

describe("declared-type derivation + picker accept", () => {
  test("extension mapping is case-insensitive and covers .db", () => {
    assert.equal(deriveDeclaredType("data.csv"), "CSV");
    assert.equal(deriveDeclaredType("DATA.CSV"), "CSV");
    assert.equal(deriveDeclaredType("export.XLSX"), "XLSX");
    assert.equal(deriveDeclaredType("db.sqlite"), "SQLITE");
    assert.equal(deriveDeclaredType("dump.db"), "SQLITE");
    assert.equal(deriveDeclaredType("archive.sqlite3"), "SQLITE");
  });

  test("unknown extensions map to null (server stays the verdict)", () => {
    assert.equal(deriveDeclaredType("photo.jpg"), null);
    assert.equal(deriveDeclaredType("noext"), null);
  });

  test("the picker accept attribute covers the whole closed universe", () => {
    assert.equal(FILE_PICKER_ACCEPT, ".csv,.xlsx,.sqlite,.db,.sqlite3");
  });
});

describe("size policy display", () => {
  test("the default cap mirrors the backend 200 MB setting", () => {
    assert.equal(DEFAULT_MAX_UPLOAD_BYTES, 200 * 1024 * 1024);
    assert.equal(formatFileSize(DEFAULT_MAX_UPLOAD_BYTES), "200 MB");
  });
});
