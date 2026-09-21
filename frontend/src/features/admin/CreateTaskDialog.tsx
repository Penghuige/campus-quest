"use client";
/**
 * Create-task dialog (spec §6; patterns §6 forms): the DRAFT-creation
 * form over a native `<dialog>`. The fields are the publish-validation
 * set (binding constraint): title/description/reward band, deadline mode
 * + its mode-conditional field, claim cutoff, file types, size cap, and
 * the draft-legal submission-schema JSON + version.
 *
 * Client mirrors are CONVENIENCE ONLY (`validateTaskForm`): a bad band
 * never leaves the browser; every business verdict (unsupported values,
 * the FIXED-deadline/cutoff interplay, schema rules at publish) is the
 * server's typed envelope, rendered code-keyed under the form. Success
 * hands the CREATED task (the server echo) to the parent.
 */
import { useEffect, useRef, useState, type FormEvent } from "react";

import { FormErrorSummary } from "@/features/auth/FormErrorSummary";
import type { AuthErrorView } from "@/features/auth/errors";
import { isApiError } from "@/lib/errors";

import {
  createTeacherTask,
  ALLOWED_TASK_FILE_TYPES,
  MAX_TASK_FILE_SIZE_BYTES,
  type TeacherTaskDto,
} from "./teacherApi";
import {
  DEADLINE_MODE_OPTIONS,
  EMPTY_TASK_FORM,
  TASK_RARITY_OPTIONS,
  taskFormToBody,
  type TaskFormErrors,
  type TaskFormValues,
} from "./teacherView";

export interface CreateTaskDialogProps {
  open: boolean;
  onClose: () => void;
  /** Boundary hook: the server-echoed DRAFT lands in the caller's list. */
  onCreated: (task: TeacherTaskDto) => void;
}

export function CreateTaskDialog({ open, onClose, onCreated }: CreateTaskDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [values, setValues] = useState<TaskFormValues>(EMPTY_TASK_FORM);
  const [fieldErrors, setFieldErrors] = useState<TaskFormErrors>({});
  const [summary, setSummary] = useState<AuthErrorView | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Controlled open/close over the native dialog; every fresh open resets.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null) {
      return;
    }
    if (open && !dialog.open) {
      setValues(EMPTY_TASK_FORM);
      setFieldErrors({});
      setSummary(null);
      setSubmitting(false);
      dialog.showModal();
    }
    if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  function setField<K extends keyof TaskFormValues>(key: K, value: TaskFormValues[K]) {
    setValues((previous) => ({ ...previous, [key]: value }));
    if (fieldErrors[key as keyof TaskFormErrors] !== undefined) {
      setFieldErrors((previous) => {
        const next = { ...previous };
        delete next[key as keyof TaskFormErrors];
        return next;
      });
    }
  }

  function toggleFileType(type: string) {
    setField(
      "fileTypes",
      values.fileTypes.includes(type as TaskFormValues["fileTypes"][number])
        ? values.fileTypes.filter((item) => item !== type)
        : [...values.fileTypes, type as TaskFormValues["fileTypes"][number]],
    );
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) {
      return;
    }
    setSummary(null);
    const result = taskFormToBody(values, Date.now());
    setFieldErrors(result.errors);
    if (!result.ok || result.body === undefined) {
      return;
    }
    setSubmitting(true);
    try {
      const task = await createTeacherTask(result.body);
      onCreated(task);
    } catch (cause) {
      setSummary(describeCreateError(cause));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="dialog dialog-wide"
      aria-labelledby="create-task-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!submitting) {
          onClose();
        }
      }}
      onClick={(event) => {
        if (event.target === dialogRef.current && !submitting) {
          onClose();
        }
      }}
    >
      <form className="dialog-body" onSubmit={onSubmit} noValidate>
        <h3 id="create-task-title" className="dialog-title">
          新建任务
        </h3>
        <p className="field-hint">
          创建后任务为草稿状态：需要先在详情页导入任务单元，再通过发布校验。
        </p>

        <FormErrorSummary view={summary} />

        <div className="field">
          <label className="field-label" htmlFor="task-title">
            任务标题
          </label>
          <input
            id="task-title"
            className="input"
            value={values.title}
            onChange={(event) => setField("title", event.target.value)}
            maxLength={255}
            required
            aria-invalid={fieldErrors.title !== undefined}
            disabled={submitting}
          />
          {fieldErrors.title !== undefined ? (
            <p className="field-error">{fieldErrors.title}</p>
          ) : null}
        </div>

        <div className="field">
          <label className="field-label" htmlFor="task-description">
            任务描述
          </label>
          <textarea
            id="task-description"
            className="input"
            rows={3}
            value={values.description}
            onChange={(event) => setField("description", event.target.value)}
            required
            aria-invalid={fieldErrors.description !== undefined}
            disabled={submitting}
          />
          {fieldErrors.description !== undefined ? (
            <p className="field-error">{fieldErrors.description}</p>
          ) : null}
        </div>

        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="task-reward">
              基础奖励积分
            </label>
            <input
              id="task-reward"
              className="input"
              inputMode="numeric"
              value={values.baseRewardPoints}
              onChange={(event) => setField("baseRewardPoints", event.target.value)}
              required
              aria-invalid={fieldErrors.baseRewardPoints !== undefined}
              disabled={submitting}
            />
            {fieldErrors.baseRewardPoints !== undefined ? (
              <p className="field-error">{fieldErrors.baseRewardPoints}</p>
            ) : null}
          </div>

          <div className="field">
            <label className="field-label" htmlFor="task-rarity">
              稀有度
            </label>
            <select
              id="task-rarity"
              className="input"
              value={values.rarity}
              onChange={(event) => setField("rarity", event.target.value)}
              disabled={submitting}
            >
              {TASK_RARITY_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="task-deadline-mode">
              截止模式
            </label>
            <select
              id="task-deadline-mode"
              className="input"
              value={values.deadlineMode}
              onChange={(event) =>
                setField(
                  "deadlineMode",
                  event.target.value === "RELATIVE" ? "RELATIVE" : "FIXED",
                )
              }
              disabled={submitting}
            >
              {DEADLINE_MODE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          {values.deadlineMode === "FIXED" ? (
            <div className="field">
              <label className="field-label" htmlFor="task-deadline">
                固定截止时间
              </label>
              <input
                id="task-deadline"
                className="input"
                type="datetime-local"
                value={values.fixedDeadlineLocal}
                onChange={(event) => setField("fixedDeadlineLocal", event.target.value)}
                required
                aria-invalid={fieldErrors.deadline !== undefined}
                disabled={submitting}
              />
            </div>
          ) : (
            <div className="field">
              <label className="field-label" htmlFor="task-duration">
                提交时限（分钟）
              </label>
              <input
                id="task-duration"
                className="input"
                inputMode="numeric"
                value={values.durationMinutes}
                onChange={(event) => setField("durationMinutes", event.target.value)}
                required
                aria-invalid={fieldErrors.deadline !== undefined}
                disabled={submitting}
              />
            </div>
          )}
        </div>
        {fieldErrors.deadline !== undefined ? (
          <p className="field-error">{fieldErrors.deadline}</p>
        ) : null}

        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="task-cutoff">
              领取截止（发布后分钟数）
            </label>
            <input
              id="task-cutoff"
              className="input"
              inputMode="numeric"
              value={values.claimCutoffMinutes}
              onChange={(event) => setField("claimCutoffMinutes", event.target.value)}
              aria-invalid={fieldErrors.claimCutoffMinutes !== undefined}
              disabled={submitting}
            />
            {fieldErrors.claimCutoffMinutes !== undefined ? (
              <p className="field-error">{fieldErrors.claimCutoffMinutes}</p>
            ) : null}
          </div>

          <div className="field">
            <label className="field-label" htmlFor="task-size">
              单文件上限（MB）
            </label>
            <input
              id="task-size"
              className="input"
              inputMode="decimal"
              value={values.maxFileSizeMb}
              onChange={(event) => setField("maxFileSizeMb", event.target.value)}
              aria-invalid={fieldErrors.maxFileSizeMb !== undefined}
              disabled={submitting}
            />
            {fieldErrors.maxFileSizeMb !== undefined ? (
              <p className="field-error">{fieldErrors.maxFileSizeMb}</p>
            ) : null}
          </div>
        </div>

        <fieldset className="field">
          <legend className="field-label">允许的文件类型</legend>
          <div className="perm-checks" role="group" aria-label="允许的文件类型">
            {ALLOWED_TASK_FILE_TYPES.map((type) => (
              <label key={type} className="identity-option">
                <input
                  type="checkbox"
                  checked={values.fileTypes.includes(type)}
                  onChange={() => toggleFileType(type)}
                  disabled={submitting}
                />
                <span>{type}</span>
              </label>
            ))}
          </div>
          {fieldErrors.fileTypes !== undefined ? (
            <p className="field-error">{fieldErrors.fileTypes}</p>
          ) : null}
          <p className="field-hint">平台上限 {MAX_TASK_FILE_SIZE_BYTES / (1024 * 1024)} MB，发布前至少配置一种类型。</p>
        </fieldset>

        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="task-schema">
              提交校验 schema（JSON，发布前必填）
            </label>
            <textarea
              id="task-schema"
              className="input mono"
              rows={3}
              placeholder='{"columns": ["platform", "keyword"]}'
              value={values.submissionSchema}
              onChange={(event) => setField("submissionSchema", event.target.value)}
              aria-invalid={fieldErrors.submissionSchema !== undefined}
              disabled={submitting}
            />
            {fieldErrors.submissionSchema !== undefined ? (
              <p className="field-error">{fieldErrors.submissionSchema}</p>
            ) : null}
          </div>

          <div className="field">
            <label className="field-label" htmlFor="task-schema-version">
              schema 版本
            </label>
            <input
              id="task-schema-version"
              className="input"
              inputMode="numeric"
              placeholder="1"
              value={values.submissionSchemaVersion}
              onChange={(event) =>
                setField("submissionSchemaVersion", event.target.value)
              }
              disabled={submitting}
            />
          </div>
        </div>

        <div className="dialog-actions">
          <SubmitButton submitting={submitting} />
          <button
            type="button"
            className="btn btn-secondary"
            onClick={onClose}
            disabled={submitting}
          >
            取消
          </button>
        </div>
      </form>
    </dialog>
  );
}

function SubmitButton({ submitting }: { submitting: boolean }) {
  return (
    <button type="submit" className="btn btn-primary" disabled={submitting} aria-busy={submitting}>
      {submitting ? <span className="spinner" aria-hidden="true" /> : null}
      <span>创建草稿</span>
    </button>
  );
}

/** Code-keyed error view for the create mutation (network vs envelope). */
export function describeCreateError(error: unknown): AuthErrorView {
  if (!isApiError(error)) {
    return { summary: "网络异常，请检查连接后重试", fieldErrors: {}, requestId: null };
  }
  return {
    summary: error.message || "创建失败，请稍后重试",
    fieldErrors: {},
    requestId: error.requestId,
  };
}
