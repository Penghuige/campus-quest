"use client";
/**
 * Create-task dialog (spec §6; patterns §6 forms): the DRAFT-creation
 * form over a native `<dialog>`. The fields are the publish-validation
 * set (binding constraint) rendered by the shared `TaskFormFields`
 * (the edit dialog reuses them under the V1 edit rule).
 *
 * Client mirrors are CONVENIENCE ONLY (`validateTaskForm`): a bad band
 * never leaves the browser; every business verdict (unsupported values,
 * the FIXED-deadline/cutoff interplay, schema rules at publish) is the
 * server's typed envelope, rendered code-keyed under the form. Success
 * hands the CREATED task (the server echo) to the parent.
 */
import { useEffect, useRef, useState, type FormEvent } from "react";

import { FormErrorSummary } from "@/features/auth/FormErrorSummary";

import { createTeacherTask, type TeacherTaskDto } from "./teacherApi";
import {
  describeTaskMutationError,
  EMPTY_TASK_FORM,
  taskFormToBody,
  type TaskFormErrors,
  type TaskFormFieldKey,
  type TaskFormValues,
} from "./teacherView";
import { TaskFormFields } from "./TaskFormFields";

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
  const [summary, setSummary] = useState<ReturnType<typeof describeTaskMutationError> | null>(null);
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

  function setField<K extends TaskFormFieldKey>(key: K, value: TaskFormValues[K]) {
    setValues((previous) => ({ ...previous, [key]: value }));
    if (fieldErrors[key as keyof TaskFormErrors] !== undefined) {
      setFieldErrors((previous) => {
        const next = { ...previous };
        delete next[key as keyof TaskFormErrors];
        return next;
      });
    }
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
      setSummary(describeTaskMutationError(cause, "创建失败，请稍后重试"));
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

        <TaskFormFields
          values={values}
          fieldErrors={fieldErrors}
          disabled={submitting}
          setField={setField}
        />

        <div className="dialog-actions">
          <button
            type="submit"
            className="btn btn-primary"
            disabled={submitting}
            aria-busy={submitting}
          >
            {submitting ? <span className="spinner" aria-hidden="true" /> : null}
            <span>创建草稿</span>
          </button>
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
