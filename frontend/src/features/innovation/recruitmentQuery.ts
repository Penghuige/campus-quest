/** Keep campus query parameters separate from owner drafts and form data. */
export function recruitmentQuery(values: Record<string, string | string[] | undefined>, detail = false): string {
  const params = new URLSearchParams();
  const keys = detail ? ["achievement_offset", "recruitment_offset"] : ["q", "direction", "stage", "beginner_friendly", "recruiting_only", "offset"];
  for (const key of keys) { const value = values[key]; if (typeof value === "string" && value !== "") params.set(key, value); }
  return params.toString();
}
