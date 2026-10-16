/**
 * The submission CSV template download (owner request 2026-10-16;
 * contract from the reviewer's task brief): GET
 * /api/v1/tasks/{id}/submission-template answers a text/csv ATTACHMENT
 * (BOM-prefixed UTF-8 so Excel opens the Chinese headers correctly) —
 * the student-side button that hands over exactly the columns this
 * task's submission schema expects.
 *
 * Access shape: the endpoint rides the authenticated fetch (cookie +
 * bearer), so the button fetches the blob — the avatar fetch's pattern
 * (a direct fetch with the memory-only bearer) — and hands the browser
 * an object URL with the response's filename. The object URL revokes
 * after the click (no leak).
 */
import { getAccessToken } from "@/lib/accessToken";
import { resolveApiPath } from "@/lib/apiBase";
import { toApiError } from "@/lib/errors";

/** Response metadata the caller surfaces (the filename hint). */
export interface TemplateDownload {
  blob: Blob;
  filename: string;
}

/** Pull the filename out of Content-Disposition (attachment; ...). */
export function templateFilenameFromDisposition(
  disposition: string | null,
  fallback: string,
): string {
  if (disposition === null) {
    return fallback;
  }
  const match = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(disposition);
  return match === null ? fallback : decodeURIComponent(match[1].replace(/"$/, ""));
}

/** Fetch this task's submission template as a downloadable blob. */
export async function downloadSubmissionTemplate(
  taskId: string,
): Promise<TemplateDownload> {
  const headers = new Headers({ Accept: "text/csv" });
  const token = getAccessToken();
  if (token !== null) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(
    resolveApiPath(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/submission-template`,
    ),
    { headers, credentials: "include" },
  );
  if (!response.ok) {
    throw toApiError(response.status, null, response.headers.get("X-Request-ID"));
  }
  return {
    blob: await response.blob(),
    filename: templateFilenameFromDisposition(
      response.headers.get("Content-Disposition"),
      "submission-template.csv",
    ),
  };
}
