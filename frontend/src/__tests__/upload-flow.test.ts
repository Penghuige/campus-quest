/**
 * Task 4: the upload flow state machine (patterns §11) — intent/PUT/
 * complete/poll transitions plus every error branch, the bounded-backoff
 * poll schedule, and the picker pre-checks.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  deriveDeclaredType,
  type SubmissionDto,
  type ValidationDto,
} from "../features/submissions/api";
import {
  initialUploadFlowState,
  isTerminalValidationStatus,
  MAX_POLL_ATTEMPTS,
  nextPollDelayMs,
  preCheckFile,
  uploadFlowReducer,
  UPLOAD_PHASE_LABELS,
  type UploadFlowState,
} from "../features/submissions/uploadFlow";

const CLAIM = "11111111-1111-4111-8111-111111111111";

function submission(overrides: Partial<SubmissionDto> = {}): SubmissionDto {
  return {
    id: "33333333-3333-4333-8333-333333333333",
    claim_id: CLAIM,
    version: 2,
    original_filename: "data.csv",
    declared_type: "CSV",
    file_size: 512,
    submitted_at: "2026-09-21T08:00:00Z",
    validation_status: "UPLOADED",
    review_status: "PENDING_REVIEW",
    created_at: "2026-09-21T08:00:00Z",
    ...overrides,
  };
}

function validation(status: string): ValidationDto {
  return {
    submission_id: "33333333-3333-4333-8333-333333333333",
    claim_id: CLAIM,
    version: 2,
    validation_status: status,
    review_status: "PENDING_REVIEW",
    detected_type: null,
    report: null,
  };
}

function step(state: UploadFlowState, ...events: Parameters<typeof uploadFlowReducer>[1][]) {
  return events.reduce(uploadFlowReducer, state);
}

describe("happy-path transitions", () => {
  test("select -> prepare -> intent -> upload -> finalize -> validating", () => {
    const selected = uploadFlowReducer(initialUploadFlowState, {
      type: "file-selected",
      filename: "data.csv",
      fileSize: 512,
      declaredType: "CSV",
    });
    assert.equal(selected.filename, "data.csv");
    assert.equal(selected.declaredType, "CSV");
    assert.equal(selected.phase, "idle");

    const preparing = uploadFlowReducer(selected, { type: "prepare-started" });
    assert.equal(preparing.phase, "preparing");

    const uploading = uploadFlowReducer(preparing, {
      type: "intent-issued",
      intentId: "22222222-2222-4222-8222-222222222222",
    });
    assert.equal(uploading.phase, "uploading");
    assert.equal(uploading.intentId, "22222222-2222-4222-8222-222222222222");

    const withProgress = uploadFlowReducer(uploading, {
      type: "progress",
      loaded: 256,
      total: 512,
    });
    assert.equal(withProgress.phase, "uploading");
    assert.equal(withProgress.progress, 0.5);

    const done = uploadFlowReducer(withProgress, { type: "put-succeeded" });
    assert.equal(done.phase, "finalizing");
    assert.equal(done.progress, 1);

    const validating = uploadFlowReducer(done, {
      type: "finalize-succeeded",
      submission: submission(),
    });
    assert.equal(validating.phase, "validating");
    assert.equal(validating.submission?.id, "33333333-3333-4333-8333-333333333333");
    assert.equal(validating.pollAttempt, 0);
  });

  test("progress without computable length stays indeterminate", () => {
    let state = uploadFlowReducer(initialUploadFlowState, { type: "prepare-started" });
    state = uploadFlowReducer(state, {
      type: "intent-issued",
      intentId: "i-1",
    });
    state = uploadFlowReducer(state, { type: "progress", loaded: 10, total: null });
    assert.equal(state.progress, null);
  });

  test("poll ticks count attempts until a terminal status", () => {
    let state = uploadFlowReducer(initialUploadFlowState, {
      type: "finalize-succeeded",
      submission: submission(),
    });
    state = uploadFlowReducer(state, { type: "poll-sampled", validation: validation("VALIDATING") });
    assert.equal(state.phase, "validating");
    assert.equal(state.pollAttempt, 1);
    state = uploadFlowReducer(state, { type: "poll-sampled", validation: validation("UPLOADED") });
    assert.equal(state.pollAttempt, 2);
    assert.equal(state.phase, "validating");
  });

  test("VALIDATED lands in under-review with the sample retained", () => {
    let state = uploadFlowReducer(initialUploadFlowState, {
      type: "finalize-succeeded",
      submission: submission(),
    });
    const terminal = validation("VALIDATED");
    state = uploadFlowReducer(state, { type: "poll-sampled", validation: terminal });
    assert.equal(state.phase, "under-review");
    assert.equal(state.validation, terminal);
  });

  test("VALIDATION_FAILED lands in the failed phase with the sample retained", () => {
    let state = uploadFlowReducer(initialUploadFlowState, {
      type: "finalize-succeeded",
      submission: submission(),
    });
    const terminal = validation("VALIDATION_FAILED");
    state = uploadFlowReducer(state, { type: "poll-sampled", validation: terminal });
    assert.equal(state.phase, "validation-failed");
    assert.equal(state.validation, terminal);
  });
});

describe("error branches", () => {
  test("intent failure -> prepare-failed", () => {
    const state = uploadFlowReducer(initialUploadFlowState, {
      type: "failed",
      stage: "prepare",
      error: new Error("window closed"),
    });
    assert.equal(state.phase, "prepare-failed");
    assert.ok(state.error instanceof Error);
    assert.equal((state.error as Error).message, "window closed");
  });

  test("PUT failure -> upload-failed (retry re-enters preparing with a fresh intent slot)", () => {
    let state = step(
      initialUploadFlowState,
      { type: "prepare-started" },
      { type: "intent-issued", intentId: "i-1" },
      { type: "failed", stage: "upload", error: new TypeError("put failed") },
    );
    assert.equal(state.phase, "upload-failed");
    assert.equal(state.intentId, "i-1");
    state = uploadFlowReducer(state, { type: "retry-upload-started" });
    assert.equal(state.phase, "preparing");
    assert.equal(state.intentId, null);
    assert.equal(state.progress, null);
  });

  test("finalize failure -> retry-finalize KEEPS the intent (§32 replay)", () => {
    let state = step(
      initialUploadFlowState,
      { type: "prepare-started" },
      { type: "intent-issued", intentId: "i-1" },
      { type: "put-succeeded" },
      { type: "finalize-failed", error: new Error("boom") },
    );
    assert.equal(state.phase, "retry-finalize");
    assert.equal(state.intentId, "i-1");
    state = uploadFlowReducer(state, { type: "retry-finalize-started" });
    assert.equal(state.phase, "finalizing");
  });

  test("poll budget exhaustion -> poll-exhausted only from validating", () => {
    const validating = uploadFlowReducer(initialUploadFlowState, {
      type: "finalize-succeeded",
      submission: submission(),
    });
    assert.equal(uploadFlowReducer(validating, { type: "poll-exhausted" }).phase, "poll-exhausted");
    // From any other phase the event is a no-op.
    assert.equal(
      uploadFlowReducer(initialUploadFlowState, { type: "poll-exhausted" }).phase,
      "idle",
    );
  });

  test("a late event after reset never resurrects the flow", () => {
    let state = step(
      initialUploadFlowState,
      { type: "finalize-succeeded", submission: submission() },
      { type: "reset" },
    );
    assert.equal(state.phase, "idle");
    assert.equal(state.submission, null);
    state = uploadFlowReducer(state, { type: "poll-sampled", validation: validation("VALIDATED") });
    assert.equal(state.phase, "idle");
  });

  test("progress outside uploading is ignored", () => {
    const state = uploadFlowReducer(initialUploadFlowState, {
      type: "progress",
      loaded: 1,
      total: 2,
    });
    assert.equal(state.phase, "idle");
  });
});

describe("backoff poll schedule", () => {
  test("delays grow geometrically and cap at 5s", () => {
    assert.equal(nextPollDelayMs(1), 800);
    assert.equal(nextPollDelayMs(2), 1280);
    assert.ok(nextPollDelayMs(10) <= 5_000);
    assert.equal(nextPollDelayMs(50), 5_000);
  });

  test("the attempt budget is bounded", () => {
    assert.ok(MAX_POLL_ATTEMPTS >= 20 && MAX_POLL_ATTEMPTS <= 40);
  });

  test("terminal statuses stop the poll", () => {
    assert.equal(isTerminalValidationStatus("VALIDATED"), true);
    assert.equal(isTerminalValidationStatus("VALIDATION_FAILED"), true);
    assert.equal(isTerminalValidationStatus("VALIDATING"), false);
    assert.equal(isTerminalValidationStatus("UPLOADED"), false);
  });
});

describe("picker pre-checks (convenience only — server is the verdict)", () => {
  test("a universe file passes and carries its derived type", () => {
    const check = preCheckFile({ name: "data.csv", size: 10 });
    assert.deepEqual(check, { ok: true, declaredType: "CSV" });
  });

  test("an out-of-universe extension fails with typed copy", () => {
    const check = preCheckFile({ name: "photo.jpg", size: 10 });
    assert.equal(check.ok, false);
    if (!check.ok) {
      assert.equal(check.reason, "type-unknown");
      assert.match(check.message, /CSV/);
    }
  });

  test("an oversized file fails against the mirrored default cap", () => {
    const check = preCheckFile({ name: "data.csv", size: 200 * 1024 * 1024 + 1 });
    assert.equal(check.ok, false);
    if (!check.ok) {
      assert.equal(check.reason, "too-large");
      assert.match(check.message, /200 MB/);
    }
  });

  test("deriveDeclaredType integration: the pre-check uses the api mapping", () => {
    assert.equal(deriveDeclaredType("a.DB"), "SQLITE");
  });
});

describe("phase labels (design §9: product wording, no raw enums)", () => {
  test("every phase carries human copy", () => {
    for (const label of Object.values(UPLOAD_PHASE_LABELS)) {
      assert.ok(label.length > 0);
    }
    assert.equal(UPLOAD_PHASE_LABELS["under-review"], "已提交，等待老师审核");
    assert.equal(UPLOAD_PHASE_LABELS["validation-failed"], "校验未通过");
  });
});
