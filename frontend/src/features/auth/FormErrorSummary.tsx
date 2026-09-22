/**
 * Error summary region for auth forms (§10 Error; §18 accessibility):
 * announced via `role="alert"`, focusable so screen readers and keyboard
 * users land on it after a failed submit, and — for unexpected/system
 * failures only — it surfaces the request id. Color is never the only cue:
 * every error line starts with a bold marker and the message text itself.
 */
import { useEffect, useRef } from "react";

import type { AuthErrorView } from "./errors";

export interface FormErrorSummaryProps {
  view: AuthErrorView | null;
}

export function FormErrorSummary({ view }: FormErrorSummaryProps) {
  const ref = useRef<HTMLDivElement>(null);

  // A fresh summary (identity change) moves focus onto it; the alert role
  // makes the same change announce for assistive tech (patterns §18).
  useEffect(() => {
    if (view !== null) {
      ref.current?.focus();
    }
  }, [view]);

  if (view === null || (view.summary === null && view.requestId === null)) {
    return null;
  }

  return (
    <div className="alert alert-error" role="alert" tabIndex={-1} ref={ref}>
      {view.summary !== null ? (
        <p>
          <span className="alert-marker" aria-hidden="true">
            !
          </span>
          {view.summary}
        </p>
      ) : null}
      {view.requestId !== null ? (
        <p className="req-id">请求 ID：{view.requestId}</p>
      ) : null}
    </div>
  );
}
