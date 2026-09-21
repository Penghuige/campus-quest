/**
 * Upload-flow state machine (patterns §11 file-upload pattern; spec §10
 * presigned flow). PURE: a discriminated-union state plus a reducer over
 * explicit events — no timers, no fetching (the component orchestrates
 * the side effects around `dispatch`), so every transition and error
 * branch is deterministically testable.
 *
 * State model (patterns §11):
 *
 * ~~~text
 * idle -> preparing -> uploading -> finalizing -> validating
 *      -> under_review | validation_failed -> (new version) idle
 * retry-finalize: uploading landed but finalize failed -> finalize again
 * with the SAME single-use intent (server replay returns the same
 * Submission, spec §32) instead of forcing a full re-upload.
 * ~~~
 *
 * Display rule: phases surface through `uploadPhaseLabel` product wording
 * (design §9 — never raw enum names). The presigned URL itself never
 * enters this state (it is transient in the orchestrator only; spec §40).
 */
import {
  DEFAULT_MAX_UPLOAD_BYTES,
  deriveDeclaredType,
  type FileTypeKey,
  formatFileSize,
  type SubmissionDto,
  type ValidationDto,
} from "./api";

// --- phases ------------------------------------------------------------------------

export type UploadPhase =
  | "idle"
  | "preparing"
  | "uploading"
  | "finalizing"
  | "retry-finalize"
  | "validating"
  | "poll-exhausted"
  | "under-review"
  | "validation-failed"
  | "prepare-failed"
  | "upload-failed";

export interface UploadFlowState {
  phase: UploadPhase;
  filename: string | null;
  fileSize: number | null;
  declaredType: FileTypeKey | null;
  /** Single-use intent id (kept for the retry-finalize path). */
  intentId: string | null;
  /** Upload ratio 0..1; null = indeterminate (no computable length yet). */
  progress: number | null;
  /** The finalized Submission (present from `finalizing` success on). */
  submission: SubmissionDto | null;
  /** Latest validation sample; terminal states carry the §12.4 report. */
  validation: ValidationDto | null;
  /** Completed poll attempts in the current validating run. */
  pollAttempt: number;
  /** The failure behind a *-failed phase (ApiError / TypeError). */
  error: unknown;
}

export const initialUploadFlowState: UploadFlowState = {
  phase: "idle",
  filename: null,
  fileSize: null,
  declaredType: null,
  intentId: null,
  progress: null,
  submission: null,
  validation: null,
  pollAttempt: 0,
  error: null,
};

// --- events ------------------------------------------------------------------------

export type UploadFlowEvent =
  | { type: "file-selected"; filename: string; fileSize: number; declaredType: FileTypeKey }
  | { type: "prepare-started" }
  | { type: "intent-issued"; intentId: string }
  | { type: "progress"; loaded: number; total: number | null }
  | { type: "put-succeeded" }
  | { type: "finalize-succeeded"; submission: SubmissionDto }
  | { type: "finalize-failed"; error: unknown }
  | { type: "retry-finalize-started" }
  | { type: "retry-upload-started" }
  | { type: "poll-sampled"; validation: ValidationDto }
  | { type: "poll-exhausted" }
  | { type: "failed"; stage: "prepare" | "upload"; error: unknown }
  | { type: "reset" };

/**
 * Advance the machine. Unknown/illegal transitions return the state
 * unchanged (the orchestrator owns sequencing; a late event after a
 * reset must not resurrect a dead flow).
 */
export function uploadFlowReducer(
  state: UploadFlowState,
  event: UploadFlowEvent,
): UploadFlowState {
  switch (event.type) {
    case "file-selected":
      return {
        ...initialUploadFlowState,
        phase: "idle",
        filename: event.filename,
        fileSize: event.fileSize,
        declaredType: event.declaredType,
      };
    case "prepare-started":
      return { ...state, phase: "preparing", error: null, progress: null };
    case "intent-issued":
      return { ...state, phase: "uploading", intentId: event.intentId, progress: null };
    case "progress": {
      if (state.phase !== "uploading") {
        return state;
      }
      const ratio =
        event.total !== null && event.total > 0
          ? Math.min(event.loaded / event.total, 1)
          : null;
      return { ...state, progress: ratio };
    }
    case "put-succeeded":
      return { ...state, phase: "finalizing", progress: 1 };
    case "finalize-succeeded":
      return {
        ...state,
        phase: "validating",
        submission: event.submission,
        validation: null,
        pollAttempt: 0,
        error: null,
      };
    case "finalize-failed":
      return { ...state, phase: "retry-finalize", error: event.error };
    case "retry-finalize-started":
      return { ...state, phase: "finalizing", error: null };
    case "retry-upload-started":
      // A fresh intent also mints a fresh presigned URL — always safe
      // after a PUT failure (the old URL may be near expiry).
      return {
        ...state,
        phase: "preparing",
        intentId: null,
        progress: null,
        error: null,
      };
    case "poll-sampled": {
      const { validation } = event;
      if (state.phase !== "validating") {
        return state;
      }
      if (validation.validation_status === "VALIDATED") {
        return { ...state, phase: "under-review", validation };
      }
      if (validation.validation_status === "VALIDATION_FAILED") {
        return { ...state, phase: "validation-failed", validation };
      }
      return {
        ...state,
        validation,
        pollAttempt: state.pollAttempt + 1,
      };
    }
    case "poll-exhausted":
      return state.phase === "validating" ? { ...state, phase: "poll-exhausted" } : state;
    case "failed":
      return {
        ...state,
        phase: event.stage === "prepare" ? "prepare-failed" : "upload-failed",
        error: event.error,
      };
    case "reset":
      return initialUploadFlowState;
    default:
      return state;
  }
}

/** Product wording per phase (design §9; patterns §11 — no raw enums). */
export const UPLOAD_PHASE_LABELS: Record<UploadPhase, string> = {
  idle: "等待选择文件",
  preparing: "正在创建上传任务",
  uploading: "正在上传文件",
  finalizing: "正在完成提交",
  "retry-finalize": "提交未完成，可重试完成",
  validating: "正在校验文件",
  "poll-exhausted": "校验状态暂时获取不到",
  "under-review": "已提交，等待老师审核",
  "validation-failed": "校验未通过",
  "prepare-failed": "无法开始上传",
  "upload-failed": "上传失败",
};

// --- bounded validation polling (backoff schedule, pure) ---------------------------

export const POLL_INITIAL_DELAY_MS = 800;
export const POLL_BACKOFF_FACTOR = 1.6;
export const POLL_MAX_DELAY_MS = 5_000;
/** ~90s of cumulative backoff — beyond it, a manual refresh takes over. */
export const MAX_POLL_ATTEMPTS = 30;

/** Delay before poll attempt `attempt` (1-based), pure exponential backoff. */
export function nextPollDelayMs(attempt: number): number {
  const raw = POLL_INITIAL_DELAY_MS * POLL_BACKOFF_FACTOR ** Math.max(attempt - 1, 0);
  return Math.min(Math.round(raw), POLL_MAX_DELAY_MS);
}

/** The async validation statuses the poll stops on (spec §11.1). */
export function isTerminalValidationStatus(status: string): boolean {
  return status === "VALIDATED" || status === "VALIDATION_FAILED";
}

// --- client-side convenience pre-checks (patterns §6: never the verdict) -----------

export type PreCheckResult =
  | { ok: true; declaredType: FileTypeKey }
  | { ok: false; reason: "type-unknown" | "too-large"; message: string };

/**
 * Type + size pre-check for the picker. Convenience feedback only — the
 * server re-judges both against the TASK's own policy when the intent is
 * requested (FILE_TYPE_NOT_ALLOWED / FILE_TOO_LARGE; patterns §3).
 */
export function preCheckFile(
  facts: { name: string; size: number },
  maxBytes: number = DEFAULT_MAX_UPLOAD_BYTES,
): PreCheckResult {
  const declaredType = deriveDeclaredType(facts.name);
  if (declaredType === null) {
    return {
      ok: false,
      reason: "type-unknown",
      message: "不支持的文件格式，请选择 CSV、Excel（.xlsx）或 SQLite 文件",
    };
  }
  if (facts.size > maxBytes) {
    return {
      ok: false,
      reason: "too-large",
      message: `文件大小超出限制（最大 ${formatFileSize(maxBytes)}），请压缩后再试`,
    };
  }
  return { ok: true, declaredType };
}
