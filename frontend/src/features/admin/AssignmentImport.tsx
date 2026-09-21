"use client";
/**
 * Assignment import (spec §7.1 steps 1-6; design §9 File upload):
 * upload a CSV -> the server parses and pre-checks -> a preview with
 * total/valid/error counts and per-row errors -> an EXPLICIT confirm ->
 * the all-or-nothing result summary.
 *
 * Contract notes that shape this UI:
 * - the file rides the request as the RAW BODY (`Content-Type:
 *   text/csv`, ≤2 MB, ≤5000 rows, header exactly `platform,keyword`);
 * - the preview echoes COUNTS + errors only — the valid rows stay
 *   server-side, named by the single-use `preview_token` that expires
 *   (15 min); confirm inserts EXACTLY the previewed rows in one
 *   transaction, so a mid-flow change means re-upload, never a partial
 *   import;
 * - every error line is the server's own message (never parsed); the
 *   row table carries row numbers and the offending platform/keyword.
 *
 * No optimistic anything: the confirm button reflects `canConfirm` from
 * the pure preview view and stays disabled until the server answers.
 */
import { useRef, useState, type ChangeEvent } from "react";

import { SectionError } from "@/components/ui/sectionStates";
import { formatFileSize } from "@/features/submissions/api";

import {
  confirmAssignmentImport,
  previewAssignmentImport,
  type ImportConfirmDto,
  type ImportPreviewDto,
} from "./teacherApi";
import { importPreviewView } from "./teacherView";

/** Mirrors of the backend importer caps (Settings defaults). */
export const IMPORT_MAX_FILE_BYTES = 2 * 1024 * 1024;
export const IMPORT_MAX_ROWS = 5000;

export interface AssignmentImportProps {
  taskId: string;
  /**
   * Boundary hook: fires after a confirmed insert so the caller refetches
   * statistics (patterns §3 — refetch the server state at boundaries).
   */
  onImported?: (result: ImportConfirmDto) => void;
}

type ImportPhase =
  | { kind: "idle" }
  | { kind: "uploading"; fileName: string }
  | { kind: "preview"; fileName: string; preview: ImportPreviewDto }
  | { kind: "confirming"; fileName: string; preview: ImportPreviewDto }
  | { kind: "done"; fileName: string; inserted: number };

export function AssignmentImport({ taskId, onImported }: AssignmentImportProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<ImportPhase>({ kind: "idle" });
  const [error, setError] = useState<unknown>(null);
  // Client-side pre-check notices (oversize): local text, NOT a server
  // error — a SectionError here would misrender as a network fault.
  const [notice, setNotice] = useState<string | null>(null);

  async function onFileChosen(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    // Reset the input so re-choosing the SAME file re-fires onChange.
    event.target.value = "";
    if (file === undefined) {
      return;
    }
    setError(null);
    setNotice(null);
    if (file.size > IMPORT_MAX_FILE_BYTES) {
      setNotice(
        `文件 ${formatFileSize(file.size)} 超过导入上限 ${formatFileSize(IMPORT_MAX_FILE_BYTES)}，请拆分后重试`,
      );
      return;
    }
    setPhase({ kind: "uploading", fileName: file.name });
    try {
      const preview = await previewAssignmentImport(taskId, file);
      setPhase({ kind: "preview", fileName: file.name, preview });
    } catch (cause) {
      setError(cause);
      setPhase({ kind: "idle" });
    }
  }

  async function onConfirm() {
    if (phase.kind !== "preview" || phase.preview.preview_token === null) {
      return;
    }
    setError(null);
    setPhase({ kind: "confirming", fileName: phase.fileName, preview: phase.preview });
    try {
      const result = await confirmAssignmentImport(taskId, phase.preview.preview_token);
      setPhase({ kind: "done", fileName: phase.fileName, inserted: result.inserted });
      onImported?.(result);
    } catch (cause) {
      // The token is single-use and expiring: any failure returns to a
      // clean slate — re-upload is the only safe retry (spec §7.1).
      setError(cause);
      setPhase({ kind: "idle" });
    }
  }

  return (
    <section className="section" aria-label="任务单元导入">
      <div className="section-head">
        <h3 className="section-title">任务单元导入</h3>
        {phase.kind === "preview" || phase.kind === "confirming" ? (
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => setPhase({ kind: "idle" })}
            disabled={phase.kind === "confirming"}
          >
            重新选择文件
          </button>
        ) : null}
      </div>

      <p className="field-hint">
        CSV 文件（UTF-8，表头为 platform,keyword 两列），不超过{" "}
        {formatFileSize(IMPORT_MAX_FILE_BYTES)}、{IMPORT_MAX_ROWS} 行。导入前会先给出
        校验预览，确认后才写入。
      </p>

      {phase.kind === "idle" || phase.kind === "uploading" ? (
        <div className="upload-block">
          <input
            ref={inputRef}
            id="assignment-import-file"
            className="input"
            type="file"
            accept=".csv,text/csv"
            onChange={(event) => void onFileChosen(event)}
            disabled={phase.kind === "uploading"}
          />
          {phase.kind === "uploading" ? (
            <p className="upload-phase-line" aria-live="polite">
              <span className="spinner" aria-hidden="true" /> 正在校验
              {phase.fileName}…
            </p>
          ) : null}
        </div>
      ) : null}

      {phase.kind === "preview" || phase.kind === "confirming" ? (
        <ImportPreviewTable
          fileName={phase.fileName}
          preview={phase.preview}
          confirming={phase.kind === "confirming"}
          onConfirm={() => void onConfirm()}
        />
      ) : null}

      {phase.kind === "done" ? (
        <div className="alert alert-success" role="status">
          <p>
            已成功导入 {phase.inserted} 个任务单元（{phase.fileName}）。预览校验中存在问题的行未写入。
          </p>
        </div>
      ) : null}

      {notice !== null ? (
        <div className="alert alert-error" role="alert">
          <p>
            <span className="alert-marker" aria-hidden="true">!</span>
            {notice}
          </p>
        </div>
      ) : null}
      {error !== null ? <SectionError error={error} /> : null}
    </section>
  );
}

/** The §7.1 step-4 preview: counts + row-level error table + gated confirm. */
function ImportPreviewTable({
  fileName,
  preview,
  confirming,
  onConfirm,
}: {
  fileName: string;
  preview: ImportPreviewDto;
  confirming: boolean;
  onConfirm: () => void;
}) {
  const view = importPreviewView(preview);
  return (
    <div className="import-preview">
      <p className="upload-file-line">
        <span className="mono">{fileName}</span>
      </p>
      <p className="import-counts" aria-live="polite">
        {view.countsText}
        {view.canConfirm
          ? "。请核对以上结果，确认后仅导入「可导入」的行。"
          : ""}
      </p>

      {view.fileErrors.length > 0 ? (
        <div className="alert alert-error" role="alert">
          <p>
            <span className="alert-marker" aria-hidden="true">!</span>
            文件无法导入，请修正后重新上传：
          </p>
          <ul>
            {view.fileErrors.map((row) => (
              <li key={row.key}>{row.message}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {view.rowErrors.length > 0 ? (
        <>
          <h4 className="report-group-title">存在问题的行（{view.rowErrors.length}）</h4>
          <div className="table-scroll">
            <table className="staff-table" aria-label="导入校验问题行">
              <thead>
                <tr>
                  <th scope="col">位置</th>
                  <th scope="col">platform</th>
                  <th scope="col">keyword</th>
                  <th scope="col">问题</th>
                </tr>
              </thead>
              <tbody>
                {view.rowErrors.map((row) => (
                  <tr key={row.key}>
                    <td>{row.where}</td>
                    <td className="mono">{row.platform ?? "—"}</td>
                    <td className="mono">{row.keyword ?? "—"}</td>
                    <td>{row.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}

      <div className="dialog-actions import-confirm-row">
        <button
          type="button"
          className="btn btn-primary"
          onClick={onConfirm}
          disabled={!view.canConfirm || confirming}
          aria-busy={confirming}
        >
          {confirming ? <span className="spinner" aria-hidden="true" /> : null}
          <span>确认导入{preview.valid_count > 0 ? ` ${preview.valid_count} 行` : ""}</span>
        </button>
        {view.confirmHint !== null ? (
          <p className="field-hint">{view.confirmHint}</p>
        ) : null}
      </div>
    </div>
  );
}
