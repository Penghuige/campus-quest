/**
 * The CSRF double-submit reader's branch family (spec §5.6/§33.1) —
 * every shape the cookie jar can present, each with its contract
 * verdict: server-side reads nothing, a missing cookie provides no
 * token, a present cookie decodes, and a malformed escape sequence
 * must never break the request path (null, not a throw).
 */
import assert from "node:assert/strict";
import { afterEach, describe, test } from "node:test";

import { readCsrfToken } from "../lib/csrf";

interface DocumentLike {
  cookie: string;
}

const originalDocument = (globalThis as { document?: DocumentLike }).document;

function withCookieJar(cookie: string | undefined): void {
  (globalThis as { document?: DocumentLike }).document =
    cookie === undefined ? undefined : { cookie };
}

afterEach(() => {
  (globalThis as { document?: DocumentLike }).document = originalDocument;
});

describe("readCsrfToken (the double-submit pair's readable half)", () => {
  test("no document (server render) reads nothing", () => {
    withCookieJar(undefined);
    assert.equal(readCsrfToken(), null);
  });

  test("a jar without the cookie provides no token", () => {
    withCookieJar("other=1; another=2");
    assert.equal(readCsrfToken(), null);
  });

  test("the cookie decodes (including encoded characters) among siblings", () => {
    withCookieJar("a=1; csrf_token=Tok%2Fen%3D; b=2");
    assert.equal(readCsrfToken(), "Tok/en=");
  });

  test("a malformed escape sequence yields null — never a throw", () => {
    withCookieJar("csrf_token=%E0%A4%A");
    assert.equal(readCsrfToken(), null);
  });
});
