"use client";
/**
 * Edit-task dialog (spec §6.2 V1 edit rule) — the create form reused
 * over the SAME shared fields (`TaskFormFields`), prefilled from the
 * task detail, on the plan-14 Dialog primitive (components/ui/dialog;
 * the form is the content element via `DialogContent asChild`, keeping
 * the legacy grid/gap that lived on `form.dialog-body`).
 *
 * This closes the load-bearing gap: a DRAFT created without the
 * submission schema (create is DRAFT-legal) becomes publishable by
 * editing the schema in — publish requires it.
 *
 * Edit-rule handling:
 * - DRAFT: every field editable;
 * - PUBLISHED/PAUSED: contract fields (plus rarity) render FROZEN with
 *   the 发布后不可修改 hint, and the diff builder OMITS them entirely —
 *   the backend counts any provided contract field as an edit attempt,
 *   so even an unchanged value must not ride the PATCH;
 * - CLOSED/ARCHIVED: the detail page renders no edit button at all
 *   (`taskEditable`).
 *
 * The body is DIFF-ONLY (`taskFormToUpdateBody`) and validation mirrors
 * the server's provided-only scope. A frozen-field rejection (the
 * `ImmutableTaskFieldError` VALIDATION_ERROR envelope with
 * `details.fields`) renders through `describeEditError` as a named,
 * product-worded line — server-authoritative, never guessed. A no-op
 * diff closes the dialog without a request.
 */
import { useState, type FormEvent } from "react";

import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import { FormErrorSummary } from "@/features/auth/FormErrorSummary";

import { updateTeacherTask, type TeacherTaskDto } from "./teacherApi";
import {
  describeEditError,
  POST_PUBLISH_FROZEN_FIELDS,
  taskFormFromTask,
  taskFormToUpdateBody,
  type TaskFormErrors,
  type TaskFormFieldKey,
  type TaskFormValues,
} from "./teacherView";
import { TaskFormFields } from "./TaskFormFields";

export interface EditTaskDialogProps {
  task: TeacherTaskDto;
  open: boolean;
  onClose: () => void;
  /** Boundary hook: the updated detail (server echo) refetches the page. */
  onUpdated: (task: TeacherTaskDto) => void;
}

export function EditTaskDialog({ task, open, onClose, onUpdated }: EditTaskDialogProps) {
  // Mounting the inner dialog per open re-seeds the form from the
  // CURRENT task prop — the legacy reset-on-open effect's job, without
  // an effect (the parent refetches the task only after the dialog
  // closes, so no mid-open re-seed case exists).
  return open ? (
    <EditTaskDialogInner task={task} onClose={onClose} onUpdated={onUpdated} />
  ) : null;
}
import { Button } from "@/components/ui/button";

function EditTaskDialogInner({
  task,
  onClose,
  onUpdated,
}: Omit<EditTaskDialogProps, "open">) {
  const [values, setValues] = useState<TaskFormValues>(() => taskFormFromTask(task));
  const [fieldErrors, setFieldErrors] = useState<TaskFormErrors>({});
  const [summary, setSummary] = useState<ReturnType<typeof describeEditError> | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const frozen: ReadonlySet<TaskFormFieldKey> =
    task.status === "DRAFT" ? new Set() : POST_PUBLISH_FROZEN_FIELDS;

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
    const result = taskFormToUpdateBody(values, task, Date.now());
    setFieldErrors(result.errors);
    if (!result.ok || result.body === undefined) {
      return;
    }
    if (Object.keys(result.body).length === 0) {
      // Nothing changed: no request, just close (the server would no-op).
      onClose();
      return;
    }
    setSubmitting(true);
    try {
      const updated = await updateTeacherTask(task.id, result.body);
      onUpdated(updated);
    } catch (cause) {
      // Server-authoritative: a frozen-field attempt that slipped past
      // the disabled inputs renders the named-field line here.
      setSummary(describeEditError(cause));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next && !submitting) {
          onClose();
        }
      }}
    >
      <DialogContent asChild size="wide" aria-labelledby="edit-task-title">
        <form onSubmit={onSubmit} noValidate>
          <DialogTitle id="edit-task-title">编辑任务</DialogTitle>
          <p className="field-hint">
            {task.status === "DRAFT"
              ? "草稿阶段可修改全部字段；提交校验 schema 与版本在发布前必须配置。"
              : "任务已发布：仅标题与描述可修改，契约字段（奖励、截止、文件策略、schema）已冻结。"}
          </p>

          <FormErrorSummary view={summary} />

          <TaskFormFields
            values={values}
            fieldErrors={fieldErrors}
            disabled={submitting}
            frozen={frozen}
            setField={setField}
          />

          <DialogFooter>
            <Button
              type="submit"
              variant="primary"
              disabled={submitting}
              aria-busy={submitting}
            >
              {submitting ? <span className="spinner" aria-hidden="true" /> : null}
              <span>保存修改</span>
            </Button>
            <Button
              variant="secondary"
              onClick={onClose}
              disabled={submitting}
            >
              取消
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
