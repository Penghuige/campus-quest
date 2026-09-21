/**
 * Typed wrappers for the student submission endpoints (spec §10, §28;
 * backend `app/modules/submissions/router.py` + `schemas.py`).
 * Shapes come from the GENERATED OpenAPI types in `lib/api/schema`.
 *
 * Presigned flow (spec §10): `createUploadIntent` gets the single-use
 * grant, the browser PUTs the file STRAIGHT to storage via
 * `putFileToPresignedUrl` (no auth material on that request — the
 * signature in the URL IS the authorization; the Content-Type is PINNED
 * to the declared type's MIME because the provider rejects any other),
 * then `completeUpload` finalizes and the async validation runs. The
 * presigned URL is TRANSIENT STATE ONLY: it is never rendered and never
 * logged (spec §40 — no object-storage paths on any student surface).
 */
import { apiRequest } from "@/lib/api";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];

/** `UploadIntentResponse` — intent id + short-lived presigned PUT URL. */
export type UploadIntentDto = Schemas["UploadIntentResponse"];

/** `SubmissionPublic` — the privacy-safe submission DTO (no object key). */
export type SubmissionDto = Schemas["SubmissionPublic"];

/** `SubmissionValidationResponse` — status projections + the §12.4 report. */
export type ValidationDto = Schemas["SubmissionValidationResponse"];

/** `ValidationReportPayload` — the persisted §12.4 report, as-is. */
export type ValidationReportDto = Schemas["ValidationReportPayload"];

/** `ValidationFindingPayload` — one bounded error/warning sample. */
export type ValidationFindingDto = Schemas["ValidationFindingPayload"];

// --- file-type universe (backend submissions `FileType`; spec §10/§12) ------------

/** The closed upload file-type universe (never a task-specific subset list). */
export const FILE_TYPES = ["CSV", "XLSX", "SQLITE"] as const;
export type FileTypeKey = (typeof FILE_TYPES)[number];

/** Extensions accepted by the picker, per declared type. */
const FILE_TYPE_EXTENSIONS: Record<FileTypeKey, readonly string[]> = {
  CSV: [".csv"],
  XLSX: [".xlsx"],
  SQLITE: [".sqlite", ".db", ".sqlite3"],
};

/** `accept` attribute for the file picker over the whole universe. */
export const FILE_PICKER_ACCEPT: string = FILE_TYPES.flatMap(
  (type) => FILE_TYPE_EXTENSIONS[type],
).join(",");

/** Human label per type (design §14: concrete formats, not raw enum names). */
export const FILE_TYPE_LABELS: Record<FileTypeKey, string> = {
  CSV: "CSV",
  XLSX: "Excel（.xlsx）",
  SQLITE: "SQLite",
};

/**
 * MIME pinned on every presigned PUT per declared type — must match the
 * backend's `DECLARED_TYPE_CONTENT_TYPES` exactly, or the provider
 * rejects the PUT (spec §10 declared-type pinning).
 */
const DECLARED_TYPE_CONTENT_TYPES: Record<FileTypeKey, string> = {
  CSV: "text/csv",
  XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  SQLITE: "application/vnd.sqlite3",
};

/**
 * Derive the declared type from a filename's extension, or null when the
 * extension is outside the closed universe. The backend never reads the
 * filename for its type decision — this is picker convenience only.
 */
export function deriveDeclaredType(filename: string): FileTypeKey | null {
  const lower = filename.toLowerCase();
  for (const type of FILE_TYPES) {
    if (FILE_TYPE_EXTENSIONS[type].some((ext) => lower.endsWith(ext))) {
      return type;
    }
  }
  return null;
}

/**
 * The global default upload cap, mirroring backend
 * `Settings.max_upload_bytes_default` (200 MB). A task may configure a
 * SMALLER cap that the student contract does not carry — the local
 * pre-check is convenience only; the server's FILE_TOO_LARGE is the
 * verdict (patterns §3/§6).
 */
export const DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024;

/** Format a byte size for the size-policy hint (1 decimal, binary units). */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ["KB", "MB", "GB"] as const;
  let value = bytes;
  let unitIndex = -1;
  do {
    value /= 1024;
    unitIndex += 1;
  } while (value >= 1024 && unitIndex < units.length - 1);
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${units[unitIndex]}`;
}

// --- API wrappers (cookie-authenticated through apiRequest) -----------------------

/** The file facts the intent request needs (spec §10 step 1 body). */
export interface UploadFileFacts {
  name: string;
  size: number;
}

/** Request the single-use presigned upload grant (POST /submissions/upload-intent). */
export function createUploadIntent(
  claimId: string,
  file: UploadFileFacts,
  declaredType: FileTypeKey,
): Promise<UploadIntentDto> {
  return apiRequest<UploadIntentDto>("/api/v1/submissions/upload-intent", {
    method: "POST",
    body: {
      claim_id: claimId,
      filename: file.name,
      declared_type: declaredType,
      size: file.size,
    },
  });
}

/** Finalize the upload and hand off to async validation (spec §10 steps 5-8). */
export function completeUpload(intentId: string): Promise<SubmissionDto> {
  return apiRequest<SubmissionDto>("/api/v1/submissions/upload-complete", {
    method: "POST",
    body: { intent_id: intentId },
  });
}

/** The owner's validation view (GET /submissions/{id}/validation). */
export function getValidation(
  submissionId: string,
  init: { signal?: AbortSignal } = {},
): Promise<ValidationDto> {
  return apiRequest<ValidationDto>(
    `/api/v1/submissions/${encodeURIComponent(submissionId)}/validation`,
    { signal: init.signal },
  );
}

// --- direct-to-storage PUT (the ONLY request that bypasses apiRequest) ------------

/** Progress sample from the presigned PUT (ratio 0..1 when computable). */
export interface PutProgress {
  loaded: number;
  total: number | null;
}

/**
 * Minimal transport port for `putFileToPresignedUrl` — injectable so unit
 * tests pin the request shape (method, pinned Content-Type, NO auth
 * headers, no cookies) without a browser XMLHttpRequest.
 */
export interface PutTransport {
  put(
    url: string,
    body: ArrayBuffer,
    contentType: string,
    onProgress: (sample: PutProgress) => void,
    signal: AbortSignal | undefined,
  ): Promise<void>;
}

/** Error raised by the presigned PUT for any non-2xx storage answer. */
export class StoragePutError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`对象存储上传失败（HTTP ${status}）`);
    this.name = "StoragePutError";
    this.status = status;
  }
}

/**
 * PUT the file bytes straight to the short-lived presigned URL (spec §10).
 *
 * - XHR (not fetch): upload progress via `xhr.upload.onprogress` is
 *   trivial here and nonexistent on fetch; when the storage never sends
 *   computable length the caller renders an indeterminate spinner.
 * - NO Authorization header, NO CSRF header, NO cookies: the URL's
 *   signature is the authorization, and attaching credentials would leak
 *   API auth material to the storage origin.
 * - The Content-Type is pinned to the declared type's MIME: the provider
 *   rejects a PUT carrying any other value.
 * - CORS preflight (deployment contract): a non-safelisted
 *   Content-Type makes this cross-origin PUT a NON-simple request, so
 *   the browser sends an OPTIONS preflight BEFORE any bytes leave; the
 *   storage provider's CORS configuration must allow the PUT method and
 *   exactly this Content-Type (the e2e mock route mirrors the same
 *   shape). A refused preflight surfaces as the generic `onerror`
 *   network failure below — nothing in this transport can bypass it.
 */
export function putFileToPresignedUrl(
  url: string,
  file: Blob,
  declaredType: FileTypeKey,
  options: {
    onProgress?: (sample: PutProgress) => void;
    signal?: AbortSignal;
  } = {},
  transport: PutTransport = xhrPutTransport,
): Promise<void> {
  return file
    .arrayBuffer()
    .then((body) =>
      transport.put(
        url,
        body,
        DECLARED_TYPE_CONTENT_TYPES[declaredType],
        (sample) => options.onProgress?.(sample),
        options.signal,
      ),
    );
}

/** Browser XHR binding of the transport port. */
const xhrPutTransport: PutTransport = {
  put(url, body, contentType, onProgress, signal) {
    return new Promise<void>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", url);
      xhr.setRequestHeader("Content-Type", contentType);
      xhr.responseType = "text";

      const onAbort = () => {
        xhr.abort();
      };
      signal?.addEventListener("abort", onAbort);

      xhr.upload.onprogress = (event) => {
        onProgress({
          loaded: event.loaded,
          total: event.lengthComputable ? event.total : null,
        });
      };
      xhr.onerror = () => {
        signal?.removeEventListener("abort", onAbort);
        reject(new TypeError("对象存储上传失败，请检查网络连接"));
      };
      xhr.onabort = () => {
        signal?.removeEventListener("abort", onAbort);
        reject(new DOMException("Aborted", "AbortError"));
      };
      xhr.onload = () => {
        signal?.removeEventListener("abort", onAbort);
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve();
        } else {
          reject(new StoragePutError(xhr.status));
        }
      };
      xhr.send(body);
    });
  },
};
