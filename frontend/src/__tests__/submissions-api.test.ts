/**
 * Task 4: submission wire contract against fetch/transport stubs — the
 * intent/complete/validation request shapes (spec §10/§28), the
 * presigned PUT's backend-signed shape (every returned client header
 * echoed verbatim incl. `If-None-Match: *` and the pinned Content-Type,
 * a body of exactly `pinned_content_length` bytes, NO auth material —
 * the URL signature is the authorization), the local byte-pin guard,
 * and the extension -> declared-type mapping.
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
  PinnedLengthMismatchError,
  type PutTransport,
  type UploadIntentDto,
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

/** A backend-shaped intent grant (the signing contract rides it). */
function intent(overrides: Partial<UploadIntentDto> = {}): UploadIntentDto {
  return {
    intent_id: "22222222-2222-4222-8222-222222222222",
    upload_url: "https://storage.example/signed",
    expires_at: "2026-09-21T09:00:00Z",
    headers: {
      "If-None-Match": "*",
      "Content-Type": "text/csv",
    },
    pinned_content_length: 14,
    ...overrides,
  };
}

describe("upload intent wire", () => {
  test("posts claim_id/filename/declared_type/size to the §10 endpoint", async () => {
    stubFetch(JSON.stringify(intent()), 201);
    const issued = await createUploadIntent(CLAIM_ID, { name: "data.csv", size: 1024 }, "CSV");
    assert.equal(recorded?.url, "/api/v1/submissions/upload-intent");
    assert.equal(recorded?.method, "POST");
    assert.deepEqual(JSON.parse(recorded?.body ?? "{}"), {
      claim_id: CLAIM_ID,
      filename: "data.csv",
      declared_type: "CSV",
      size: 1024,
    });
    assert.equal(issued.intent_id, "22222222-2222-4222-8222-222222222222");
    // The signing contract passes through for the PUT to echo.
    assert.equal(issued.headers["If-None-Match"], "*");
    assert.equal(issued.pinned_content_length, 14);
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

describe("presigned PUT consumes the backend signing contract", () => {
  type RecordedPut = {
    url: string;
    body: ArrayBuffer;
    headers: Record<string, string>;
    onProgress: unknown;
    signal: unknown;
  };

  function fakeTransport(): { transport: PutTransport; calls: RecordedPut[] } {
    const calls: RecordedPut[] = [];
    return {
      calls,
      transport: {
        async put(url, body, headers, onProgress, signal) {
          calls.push({ url, body, headers, onProgress, signal });
          onProgress({ loaded: 5, total: 10 });
        },
      },
    };
  }

  test("echoes EVERY returned client header verbatim; body bytes == pin; NO auth material", async () => {
    const { transport, calls } = fakeTransport();
    const csv = new Blob(["platform,date\n"]); // 14 bytes == pinned_content_length
    const progress: number[] = [];
    await putFileToPresignedUrl(
      intent(),
      csv,
      { onProgress: (sample) => progress.push(sample.total === null ? -1 : sample.loaded / sample.total) },
      transport,
    );
    assert.equal(calls.length, 1);
    assert.equal(calls[0]?.url, "https://storage.example/signed");
    // Every signed header rides the PUT exactly as returned — the client
    // keeps no signing-policy copy of its own.
    assert.deepEqual(calls[0]?.headers, {
      "If-None-Match": "*",
      "Content-Type": "text/csv",
    });
    assert.equal(calls[0]?.headers["If-None-Match"], "*"); // write-once pin
    assert.equal(calls[0]?.headers["Content-Type"], "text/csv"); // pinned type
    // Body byte count == the signed pin (a browser frames Content-Length
    // itself from exactly this body; the signature holds).
    assert.ok(calls[0]?.body instanceof ArrayBuffer);
    assert.equal(calls[0]?.body.byteLength, 14);
    // The presigned PUT is a bare signed request: no Authorization, no
    // CSRF header, no cookie credential flag anywhere in the header set.
    const echoed: Record<string, string> = calls[0]?.headers ?? {};
    for (const forbidden of ["Authorization", "X-CSRF-Token", "Cookie"]) {
      assert.equal(echoed[forbidden], undefined, `${forbidden} must not ride the PUT`);
    }
    // Progress maps straight through to the caller.
    assert.deepEqual(progress, [0.5]);
  });

  test("the echo follows the backend's contract, not any frontend MIME table", async () => {
    // A backend that signs a DIFFERENT type/pin drives the PUT — proof
    // the shape comes from the intent response alone.
    const { transport, calls } = fakeTransport();
    const xlsxBytes = new Uint8Array([1, 2, 3, 4]);
    await putFileToPresignedUrl(
      intent({
        headers: {
          "If-None-Match": "*",
          "Content-Type":
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
        pinned_content_length: 4,
      }),
      new Blob([xlsxBytes]),
      {},
      transport,
    );
    assert.equal(
      calls[0]?.headers["Content-Type"],
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    );
    assert.equal(calls[0]?.body.byteLength, 4);
  });

  test("a size mismatch fails LOCALLY — no request fires (owner regression)", async () => {
    const { transport, calls } = fakeTransport();
    const error = await putFileToPresignedUrl(
      intent({ pinned_content_length: 99 }),
      new Blob(["platform,date\n"]), // 14 bytes != 99
      {},
      transport,
    ).then(
      () => assert.fail("expected rejection"),
      (e: unknown) => e,
    );
    assert.ok(error instanceof PinnedLengthMismatchError);
    assert.equal(error.actualBytes, 14);
    assert.equal(error.pinnedBytes, 99);
    assert.equal(calls.length, 0); // nothing left the browser
  });

  test("a transport failure rejects (the flow's upload-failed branch)", async () => {
    const failing: PutTransport = {
      async put() {
        throw new TypeError("network down");
      },
    };
    const error = await putFileToPresignedUrl(intent(), new Blob(["platform,date\n"]), {}, failing).then(
      () => assert.fail("expected rejection"),
      (e: unknown) => e,
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
