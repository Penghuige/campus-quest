import assert from "node:assert/strict";
import { test } from "node:test";
import { resolveAppPath } from "../lib/appPath";

test("logout full-document destination respects the app mount independently of API mount", () => {
  const originalApp = process.env.NEXT_PUBLIC_APP_BASE_PATH;
  const originalApi = process.env.NEXT_PUBLIC_API_BASE;
  try {
    process.env.NEXT_PUBLIC_APP_BASE_PATH = "";
    process.env.NEXT_PUBLIC_API_BASE = "/api-mount";
    assert.equal(resolveAppPath("/login"), "/login");
    process.env.NEXT_PUBLIC_APP_BASE_PATH = "/campus";
    assert.equal(resolveAppPath("/login"), "/campus/login");
  } finally {
    if (originalApp === undefined) delete process.env.NEXT_PUBLIC_APP_BASE_PATH;
    else process.env.NEXT_PUBLIC_APP_BASE_PATH = originalApp;
    if (originalApi === undefined) delete process.env.NEXT_PUBLIC_API_BASE;
    else process.env.NEXT_PUBLIC_API_BASE = originalApi;
  }
});
