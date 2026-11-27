import assert from "node:assert/strict";
import test from "node:test";

import { GET } from "../app/healthz/route";

test("web health route answers 200 with a dependency-free body", async () => {
  const response = GET();
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: "ok" });
});