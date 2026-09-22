"use client";
/**
 * Task lifecycle actions (spec §6.2; design §9 Buttons + §10 explicit
 * confirmations): the verbs available from the task's current status,
 * rendered from the pure `lifecycleActions` model. Close/archive/publish
 * open an explicit native-dialog confirmation whose copy states the
 * CONSEQUENCE; pause/resume are reversible and run directly.
 *
 * No optimistic anything (patterns §7): the button stays disabled until
 * the server answers, and the parent updates from the
 * `TaskTransitionResponse` verdict — a refused transition renders the
 * typed envelope copy (e.g. publish gates answering VALIDATION_ERROR or
 * TASK_NOT_CLAIMABLE) instead of a client-side guess.
 */
import { useEffect, useRef, useState, type MouseEvent } from "react";

import { SectionError } from "@/components/ui/sectionStates";

import { runTaskLifecycle, type TaskTransitionDto } from "./teacherApi";
import { lifecycleActions, type LifecycleActionView } from "./teacherView";

export interface TaskLifecycleActionsProps {
  taskId: string;
  /** Current status; drives the available verbs (the server re-decides). */
  status: string;
  /** Boundary hook: fires with the SERVER verdict on every success. */
  onTransitioned: (transition: TaskTransitionDto) => void;
  /** Render size: rows stay compact, the detail page uses standard buttons. */
  size?: "compact" | "default";
}

export function TaskLifecycleActions({
  taskId,
  status,
  onTransitioned,
  size = "default",
}: TaskLifecycleActionsProps) {
  const actions = lifecycleActions(status);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [confirming, setConfirming] = useState<LifecycleActionView | null>(null);

  async function run(action: LifecycleActionView) {
    if (pending !== null) {
      return;
    }
    setPending(action.verb);
    setError(null);
    try {
      const transition = await runTaskLifecycle(taskId, action.verb);
      onTransitioned(transition);
    } catch (cause) {
      setError(cause);
    } finally {
      setPending(null);
    }
  }

  return (
    <div className={size === "compact" ? "row-actions" : "dialog-actions"}>
      {actions.map((action) => (
        <button
          key={action.verb}
          type="button"
          className={`btn ${action.buttonClass}`}
          disabled={pending !== null}
          aria-busy={pending === action.verb}
          onClick={() => {
            if (action.confirmRequired) {
              setError(null);
              setConfirming(action);
            } else {
              void run(action);
            }
          }}
        >
          {pending === action.verb ? (
            <span className="spinner" aria-hidden="true" />
          ) : null}
          <span>{action.label}</span>
        </button>
      ))}
      {error !== null ? (
        <SectionError error={error} onRetry={undefined} />
      ) : null}
      {confirming !== null ? (
        <LifecycleConfirmDialog
          action={confirming}
          busy={pending !== null}
          onCancel={() => setConfirming(null)}
          onConfirm={() => {
            // `run` settles on both paths (typed errors render inline
            // below), so the dialog closes after the server answered.
            void run(confirming).then(() => setConfirming(null));
          }}
        />
      ) : null}
    </div>
  );
}

/** The explicit confirmation dialog (design §10: sensitive action, consequence copy). */
function LifecycleConfirmDialog({
  action,
  busy,
  onCancel,
  onConfirm,
}: {
  action: LifecycleActionView;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog !== null && !dialog.open) {
      dialog.showModal();
    }
  }, []);

  function onBackdropClick(event: MouseEvent<HTMLDialogElement>) {
    if (event.target === dialogRef.current) {
      onCancel();
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="dialog"
      aria-labelledby="lifecycle-confirm-title"
      onCancel={(event) => {
        // Keep React the source of truth over the native close.
        event.preventDefault();
        onCancel();
      }}
      onClick={onBackdropClick}
    >
      <div className="dialog-body">
        <h3 id="lifecycle-confirm-title" className="dialog-title">
          {action.confirmTitle}
        </h3>
        {action.buttonClass === "btn-danger" ? (
          <div className="alert alert-error" role="alert">
            <p>
              <span className="alert-marker" aria-hidden="true">!</span>
              {action.confirmBody}
            </p>
          </div>
        ) : (
          <p className="field-hint">{action.confirmBody}</p>
        )}
        <div className="dialog-actions">
          <button
            type="button"
            className={`btn ${action.buttonClass}`}
            onClick={onConfirm}
            disabled={busy}
            aria-busy={busy}
            autoFocus
          >
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            <span>{action.confirmLabel}</span>
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={onCancel}
            disabled={busy}
          >
            取消
          </button>
        </div>
      </div>
    </dialog>
  );
}
