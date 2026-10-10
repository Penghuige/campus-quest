"use client";
/**
 * Create-task dialog (spec §6; patterns §6 forms): the DRAFT-creation
 * form on the plan-14 Dialog primitive (components/ui/dialog) — Radix
 * supplies role=dialog/focus-trap/Escape (modality via hideOthers +
 * RemoveScroll; Radix 1.2 emits no aria-modal); the visual shell
 * carries the `.cq-dialog*` values (inherited from the retired native
 * `.dialog` contract, C3). The fields are the publish-validation set
 * (binding constraint) rendered by the shared `TaskFormFields` (the
 * edit dialog reuses them under the V1 edit rule).
 *
 * The form IS the content element (`DialogContent asChild`): the
 * form's own grid/gap must stay intact — a bare nested form would
 * collapse into one grid item of `.cq-dialog-content`'s grid and lose
 * the child gaps.
 *
 * Client mirrors are CONVENIENCE ONLY (`validateTaskForm`): a bad band
 * never leaves the browser; every business verdict (unsupported values,
 * the FIXED-deadline/cutoff interplay, schema rules at publish) is the
 * server's typed envelope, rendered code-keyed under the form. Success
 * hands the CREATED task (the server echo) to the parent.
 */
import { useState, type FormEvent } from "react";

import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import { FormErrorSummary } from "@/features/auth/FormErrorSummary";

import { createTeacherTask, type TeacherTaskDto } from "./teacherApi";
import {
  defaultSchemaFor,
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
  // Mounting the inner dialog per open starts every field fresh —
  // reset-on-open without an effect.
  return open ? (
    <CreateTaskDialogInner onClose={onClose} onCreated={onCreated} />
  ) : null;
}
import { Button } from "@/components/ui/button";

function CreateTaskDialogInner({
  onClose,
  onCreated,
}: Omit<CreateTaskDialogProps, "open">) {
  const [values, setValues] = useState<TaskFormValues>(() => ({
    ...EMPTY_TASK_FORM,
    // Defect #22: the schema starts from the per-type default template
    // (the field rides collapsed in the 高级 section) — the default
    // teacher never authors JSON by hand.
    submissionSchema: defaultSchemaFor(EMPTY_TASK_FORM.fileTypes),
    submissionSchemaVersion: "1",
  }));
  const [fieldErrors, setFieldErrors] = useState<TaskFormErrors>({});
  const [summary, setSummary] = useState<ReturnType<typeof describeTaskMutationError> | null>(null);
  const [submitting, setSubmitting] = useState(false);

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
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next && !submitting) {
          onClose();
        }
      }}
    >
      <DialogContent asChild size="wide" aria-labelledby="create-task-title">
        <form onSubmit={onSubmit} noValidate>
          <DialogTitle id="create-task-title">新建任务</DialogTitle>
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

          <DialogFooter>
            <Button
              type="submit"
              variant="primary"
              disabled={submitting}
              aria-busy={submitting}
            >
              {submitting ? <span className="spinner" aria-hidden="true" /> : null}
              <span>创建草稿</span>
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
