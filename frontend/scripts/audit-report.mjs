/** Registry transport failures are not an audit result. */
export function validateAuditReport(report) {
  const total = report?.metadata?.vulnerabilities?.total;
  if (!report || report.error || !report.vulnerabilities || typeof report.vulnerabilities !== "object" || Array.isArray(report.vulnerabilities) || !Number.isInteger(total) || total < 0) {
    throw new Error("Invalid npm audit report: registry audit did not complete");
  }
  return report;
}
