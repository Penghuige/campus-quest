import assert from "node:assert/strict";
import { test } from "node:test";
import { validateAuditReport } from "../../scripts/audit-report.mjs";

test("a failed registry response cannot be mistaken for zero vulnerabilities", () => {
  for (const report of [null, {}, { error: { code: "E404" } }, { vulnerabilities: {}, metadata: {} }]) assert.throws(() => validateAuditReport(report), /audit report/i);
});
test("a complete registry report passes shape validation", () => {
  const report = { vulnerabilities: {}, metadata: { vulnerabilities: { total: 0 } } };
  assert.equal(validateAuditReport(report), report);
});
