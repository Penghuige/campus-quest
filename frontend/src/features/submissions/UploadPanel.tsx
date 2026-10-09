"use client";
/**
 * Upload panel (patterns §11 file-upload pattern; spec §10 presigned
 * flow): picker -> intent -> browser PUT DIRECT to the presigned storage
 * URL (no auth header — the signature is the authorization) ->
 * upload-complete -> bounded-backoff polling of the async validation.
 *
 * State lives in the PURE `uploadFlow` reducer (see that module); this
 * component only orchestrates side effects around dispatch. Boundary
 * rule (patterns §3/§7): after a terminal validation outcome the parent
 * refetches the claim — a failed upload never locally mutates claim
 * state (the server's failure back-edge already restored the claim to a
 * submittable state).
 *
 * PRIVACY (spec §40): the presigned `upload_url` lives ONLY inside the
 * async run closure — it never enters component state, markup, or logs.
 */
import { useReducer, useRef, useState, useCallback, useEffect } from "react";

import {
  acceptAttributeFor,
  completeUpload,
  createUploadIntent,
  DEFAULT_MAX_UPLOAD_BYTES,
  FILE_TYPES,
  FILE_TYPE_LABELS,
  formatFileSize,
  getValidation,
  putFileToPresignedUrl,
  type FileTypeKey,
  type UploadIntentDto,
} from "./api";
import { describeSubmissionError } from "./submissionErrors";
import {
  initialUploadFlowState,
  MAX_POLL_ATTEMPTS,
  nextPollDelayMs,
  preCheckFile,
  uploadFlowReducer,
  UPLOAD_PHASE_LABELS,
  type UploadFlowState,
} from "./uploadFlow";
import { ValidationReport } from "./ValidationReport";

const BUSY_PHASES: ReadonlySet<UploadFlowState["phase"]> = new Set([
  "preparing",
  "uploading",
  "finalizing",
  "validating",
]);

/** Abortable sleep for the poll backoff. */
function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    function onAbort() {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    }
    signal.addEventListener("abort", onAbort, { once: true });
  });
}
import { Button } from "@/components/ui/button";

export function UploadPanel({
  claimId,
  onClaimChanged,
  allowedTypes,
}: {
  claimId: string;
  /** Boundary hook: the parent refetches claim state after transitions. */
  onClaimChanged: () => void;
  /** The task's upload gate (MyClaimResponse.allowed_file_types,
   * defect #12): narrows the picker's accept set and adds a
   * task-refusal pre-check branch. Absent/empty = the universe. */
  allowedTypes?: readonly string[];
}) {
  const [state, dispatch] = useReducer(uploadFlowReducer, initialUploadFlowState);
  const [preCheckMessage, setPreCheckMessage] = useState<string | null>(null);
  const [inputValue, setInputValue] = useState("");

  // The picked File (a browser object) stays OUT of the pure reducer; the
  // run controller below is the only consumer.
  const fileRef = useRef<File | null>(null);
  const runControllerRef = useRef<AbortController | null>(null);
  const onClaimChangedRef = useRef(onClaimChanged);
  useEffect(() => {
    onClaimChangedRef.current = onClaimChanged;
  });

  // Abort any in-flight run on unmount.
  useEffect(() => {
    return () => {
      runControllerRef.current?.abort();
    };
  }, []);

  /** Start a NEW run controller (aborts any previous run first). */
  const beginRun = useCallback(() => {
    runControllerRef.current?.abort();
    const controller = new AbortController();
    runControllerRef.current = controller;
    return controller;
  }, []);

  /**
   * Poll the async validation until a terminal status, the bounded
   * attempt budget runs out, or the run is aborted. A failed GET counts
   * as a spent attempt (transient tolerance) instead of failing the
   * flow: one bad poll falls through to the next backoff round, and
   * only a fully exhausted budget reaches the poll-exhausted state.
   */
  const pollValidation = useCallback(
    async (submissionId: string, signal: AbortSignal): Promise<void> => {
      for (let attempt = 1; attempt <= MAX_POLL_ATTEMPTS; attempt += 1) {
        await delay(nextPollDelayMs(attempt), signal);
        let validation;
        try {
          validation = await getValidation(submissionId, { signal });
        } catch (error) {
          if (signal.aborted) {
            throw error;
          }
          // Transient tolerance: spend the attempt, keep polling.
          continue;
        }
        dispatch({ type: "poll-sampled", validation });
        if (
          validation.validation_status === "VALIDATED" ||
          validation.validation_status === "VALIDATION_FAILED"
        ) {
          // Boundary: the claim moved (UNDER_REVIEW, or the server's
          // failure back-edge restored it) — refetch authoritative state.
          onClaimChangedRef.current();
          return;
        }
      }
      dispatch({ type: "poll-exhausted" });
    },
    [],
  );

  /** upload-complete + validation polling (also the retry-finalize path). */
  const finalize = useCallback(
    async (intentId: string, signal: AbortSignal): Promise<void> => {
      let submissionId: string;
      try {
        const submission = await completeUpload(intentId);
        submissionId = submission.id;
        dispatch({ type: "finalize-succeeded", submission });
        onClaimChangedRef.current();
      } catch (error) {
        if (signal.aborted) {
          return;
        }
        dispatch({ type: "finalize-failed", error });
        return;
      }
      try {
        await pollValidation(submissionId, signal);
      } catch {
        // Aborted run: the next run's reducer events own the state.
      }
    },
    [pollValidation],
  );

  /** The full presigned flow for the picked file (spec §10 steps 1-8). */
  const runUpload = useCallback(
    async (file: File, declaredType: FileTypeKey) => {
      const { signal } = beginRun();
      dispatch({ type: "prepare-started" });
      // The intent (presigned URL included) stays inside this closure
      // (spec §40); the PUT consumes its signing contract verbatim.
      let intent: UploadIntentDto;
      try {
        intent = await createUploadIntent(
          claimId,
          { name: file.name, size: file.size },
          declaredType,
        );
      } catch (error) {
        if (signal.aborted) {
          return;
        }
        dispatch({ type: "failed", stage: "prepare", error });
        return;
      }
      dispatch({ type: "intent-issued", intentId: intent.intent_id });
      try {
        await putFileToPresignedUrl(intent, file, {
          onProgress: (sample) =>
            dispatch({ type: "progress", loaded: sample.loaded, total: sample.total }),
          signal,
        });
      } catch (error) {
        if (signal.aborted) {
          return;
        }
        dispatch({ type: "failed", stage: "upload", error });
        return;
      }
      dispatch({ type: "put-succeeded" });
      await finalize(intent.intent_id, signal);
    },
    [beginRun, claimId, finalize],
  );

  const onFileChange = useCallback((event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] ?? null;
    // Keep the raw value so the picker re-arms (same file re-selectable).
    setInputValue(event.target.value);
    if (file === null) {
      return;
    }
    const check = preCheckFile({ name: file.name, size: file.size }, undefined, allowedTypes);
    if (!check.ok) {
      runControllerRef.current?.abort();
      dispatch({ type: "reset" });
      fileRef.current = null;
      setPreCheckMessage(check.message);
      return;
    }
    setPreCheckMessage(null);
    fileRef.current = file;
    runControllerRef.current?.abort();
    dispatch({
      type: "file-selected",
      filename: file.name,
      fileSize: file.size,
      declaredType: check.declaredType,
    });
  }, [allowedTypes]);

  const onStartUpload = useCallback(() => {
    const file = fileRef.current;
    const declaredType = state.declaredType;
    if (file === null || declaredType === null) {
      return;
    }
    void runUpload(file, declaredType);
  }, [runUpload, state.declaredType]);

  const onRetryFinalize = useCallback(() => {
    const intentId = state.intentId;
    if (intentId === null) {
      return;
    }
    const { signal } = beginRun();
    dispatch({ type: "retry-finalize-started" });
    void finalize(intentId, signal);
  }, [beginRun, finalize, state.intentId]);

  const onRetryUpload = useCallback(() => {
    const file = fileRef.current;
    const declaredType = state.declaredType;
    if (file === null || declaredType === null) {
      return;
    }
    dispatch({ type: "retry-upload-started" });
    void runUpload(file, declaredType);
  }, [runUpload, state.declaredType]);

  const onManualValidationRefresh = useCallback(() => {
    const submission = state.submission;
    if (submission === null) {
      return;
    }
    const { signal } = beginRun();
    // Re-enter validating from the known submission, then poll once more.
    dispatch({ type: "retry-finalize-started" });
    dispatch({ type: "finalize-succeeded", submission });
    void (async () => {
      try {
        await pollValidation(submission.id, signal);
      } catch {
        // aborted
      }
    })();
  }, [beginRun, pollValidation, state.submission]);

  const onReset = useCallback(() => {
    runControllerRef.current?.abort();
    fileRef.current = null;
    setPreCheckMessage(null);
    setInputValue("");
    dispatch({ type: "reset" });
  }, []);

  const busy = BUSY_PHASES.has(state.phase);
  // The task's own gate names the formats when it carries one (defect
  // #12's contract; §10.1: a document task then reads 支持格式：Word/PDF
  // 文档 with no tabular wording); absent/empty keeps the universe.
  const formatKeys =
    allowedTypes !== undefined &&
    allowedTypes.some((type) => (FILE_TYPES as readonly string[]).includes(type))
      ? allowedTypes.filter((type) =>
          (FILE_TYPES as readonly string[]).includes(type),
        )
      : FILE_TYPES;
  const allowedFormats = formatKeys
    .map((type) => FILE_TYPE_LABELS[type as FileTypeKey])
    .join(" / ");

  return (
    <section className="section upload-panel" aria-label="提交数据文件">
      <h2 className="section-title">提交数据文件</h2>
      <p className="field-hint">
        支持格式：{allowedFormats}；单个文件最大 {formatFileSize(DEFAULT_MAX_UPLOAD_BYTES)}
        （任务如有更严格的限制，以上传时的校验结果为准）。
      </p>

      <div className="field">
        <label className="field-label" htmlFor="submission-file">
          选择文件
        </label>
        <input
          id="submission-file"
          className="input"
          type="file"
          accept={acceptAttributeFor(allowedTypes)}
          value={inputValue}
          onChange={onFileChange}
          disabled={busy}
        />
        {preCheckMessage !== null ? (
          <p className="field-error" role="alert">
            {preCheckMessage}
          </p>
        ) : null}
      </div>

      {state.filename !== null ? (
        <p className="upload-file-line">
          当前文件：<span className="mono">{state.filename}</span>
          {state.fileSize !== null ? `（${formatFileSize(state.fileSize)}）` : null}
        </p>
      ) : null}

      <p className="upload-phase-line" role="status" aria-live="polite">
        {busy ? <span className="spinner" aria-hidden="true" /> : null}
        {UPLOAD_PHASE_LABELS[state.phase]}
      </p>

      {state.phase === "uploading" && state.progress !== null ? (
        <div className="progress">
          <div
            className="progress-track"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(state.progress * 100)}
            aria-label="上传进度"
          >
            <div
              className="progress-fill"
              style={{ width: `${Math.round(state.progress * 100)}%` }}
            />
          </div>
        </div>
      ) : null}
      {state.phase === "uploading" && state.progress === null ? (
        <p className="field-hint">正在建立上传连接…</p>
      ) : null}

      {state.phase === "idle" && state.filename !== null ? (
        <Button variant="primary" onClick={onStartUpload}>
          开始上传
        </Button>
      ) : null}

      {state.phase === "prepare-failed" ||
      state.phase === "upload-failed" ||
      state.phase === "retry-finalize" ? (
        <FlowErrorAlert
          error={state.error}
          phase={state.phase}
          onRetryUpload={onRetryUpload}
          onRetryFinalize={onRetryFinalize}
          onReset={onReset}
        />
      ) : null}

      {state.phase === "poll-exhausted" ? (
        <div className="alert alert-error" role="alert">
          <p>
            <span className="alert-marker" aria-hidden="true">
              !
            </span>
            校验还在进行中，暂时获取不到最新状态。
          </p>
          <p>
            <Button
              variant="secondary"
              onClick={onManualValidationRefresh}
            >
              刷新校验状态
            </Button>
          </p>
        </div>
      ) : null}

      {state.phase === "under-review" ? (
        <div className="alert alert-success" role="status">
          <p>文件已通过校验，进入人工审核队列。</p>
        </div>
      ) : null}

      {state.phase === "validation-failed" && state.validation?.report != null ? (
        <>
          <div className="alert alert-error" role="alert">
            <p>
              <span className="alert-marker" aria-hidden="true">
                !
              </span>
              提交未通过校验，请修正 {state.validation.report.errors.length} 项问题后重新上传。
            </p>
            <p className="field-hint">
              任务已回到可提交状态，重新上传会生成新的版本，不影响之前的尝试。
            </p>
            <p>
              <Button variant="primary" onClick={onReset}>
                重新上传文件
              </Button>
            </p>
          </div>
          <ValidationReport report={state.validation.report} />
        </>
      ) : null}

      {state.phase === "under-review" && state.validation?.report != null ? (
        <ValidationReport report={state.validation.report} />
      ) : null}

      {!busy &&
      state.phase !== "idle" &&
      state.phase !== "validation-failed" &&
      state.phase !== "retry-finalize" &&
      state.phase !== "prepare-failed" &&
      state.phase !== "upload-failed" ? (
        <p>
          <Button variant="secondary" onClick={onReset}>
            {state.phase === "under-review" ? "收起" : "重新选择文件"}
          </Button>
        </p>
      ) : null}
    </section>
  );
}

/** Typed failure alert with the phase-appropriate retry action. */
function FlowErrorAlert({
  error,
  phase,
  onRetryUpload,
  onRetryFinalize,
  onReset,
}: {
  error: unknown;
  phase: "prepare-failed" | "upload-failed" | "retry-finalize";
  onRetryUpload: () => void;
  onRetryFinalize: () => void;
  onReset: () => void;
}) {
  const view = describeSubmissionError(error);
  return (
    <div className="alert alert-error" role="alert">
      <p>
        <span className="alert-marker" aria-hidden="true">
          !
        </span>
        {view.message}
      </p>
      {view.requestId !== null ? (
        <p className="req-id">请求 ID：{view.requestId}</p>
      ) : null}
      <p>
        {phase === "retry-finalize" ? (
          // Patterns §11: the PUT landed — retry finalize only (the
          // server's intent replay returns the same submission, §32).
          <Button variant="primary" onClick={onRetryFinalize}>
            重试完成提交
          </Button>
        ) : null}
        {phase === "upload-failed" || phase === "prepare-failed" ? (
          <Button variant="primary" onClick={onRetryUpload}>
            重试上传
          </Button>
        ) : null}
        <Button variant="secondary" onClick={onReset}>
          重新选择文件
        </Button>
      </p>
    </div>
  );
}
