/**
 * Submission-template download pins (owner request 2026-10-16; TDD with
 * mutation checks): the Content-Disposition filename extraction and the
 * request shape (bearer + cookie + the mount prefix + the Accept
 * header). The endpoint is backend-pending; the pins hold the frozen
 * contract.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { clearAccessToken, setAccessToken } from "../lib/accessToken";
import {
  downloadSubmissionTemplate,
  templateFilenameFromDisposition,
} from "../features/submissions/templateDownload";

type Recorded = {
  url: string;
  headers: Record<string, string>;
  credentials: RequestCredentials | undefined;
};

const originalFetch = globalThis.fetch;
let recorded: Recorded | undefined;
let responseFor: () => Response;

function stubFetch(body: string, status = 200, headers: Record<string, string> = {}) {
  responseFor = () => new Response(body.length > 0 ? body : null, { status, headers });
}

describe("templateFilenameFromDisposition", () => {
  test("plain and quoted filename= forms", () => {
    assert.equal(
      templateFilenameFromDisposition('attachment; filename="t.csv"', "f.csv"),
      "t.csv",
    );
    assert.equal(
      templateFilenameFromDisposition("attachment; filename=t-1.csv", "f.csv"),
      "t-1.csv",
    );
  });

  test("UTF-8'' encoded form decodes; null/broken fall back", () => {
    assert.equal(
      templateFilenameFromDisposition(
        "attachment; filename*=UTF-8''%E6%A8%A1%E6%9D%BF.csv",
        "f.csv",
      ),
      "模板.csv",
    );
    assert.equal(templateFilenameFromDisposition(null, "f.csv"), "f.csv");
    assert.equal(
      templateFilenameFromDisposition("inline", "f.csv"),
      "f.csv",
    );
  });
});

describe("downloadSubmissionTemplate (the frozen request contract)", () => {
  test("GETs the mount-prefixed task path with bearer, cookies, and Accept: text/csv", async () => {
    stubFetch("﻿platform,keyword", 200, {
      "Content-Disposition": 'attachment; filename="tpl.csv"',
      "Content-Type": "text/csv; charset=utf-8",
    });
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const headers: Record<string, string> = {};
      new Headers(init?.headers).forEach((value, key) => {
        headers[key] = value;
      });
      recorded = {
        url: String(input),
        headers,
        credentials: init?.credentials,
      };
      return responseFor();
    }) as typeof fetch;
    try {
      setAccessToken("bearer-for-test");
      const download = await downloadSubmissionTemplate("task/uuid-1");
      assert.equal(recorded?.url, "/api/v1/tasks/task%2Fuuid-1/submission-template");
      assert.equal(recorded?.credentials, "include");
      assert.equal(recorded?.headers.accept, "text/csv");
      assert.match(recorded?.headers.authorization ?? "", /^Bearer /);
      assert.equal(download.filename, "tpl.csv");
      assert.equal(await download.blob.text(), "platform,keyword");
    } finally {
      clearAccessToken();
      globalThis.fetch = originalFetch;
    }
  });

  test("a non-2xx answers the typed ApiError envelope", async () => {
    stubFetch("", 404);
    globalThis.fetch = (async () => responseFor()) as typeof fetch;
    try {
      await assert.rejects(
        downloadSubmissionTemplate("t1"),
        (error: unknown) => {
          assert.equal((error as { status?: number }).status, 404);
          return true;
        },
      );
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
