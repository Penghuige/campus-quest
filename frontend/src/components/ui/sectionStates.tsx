"use client";
/**
 * Generic section-state primitives (design-system §10; patterns §1 —
 * components/ui owns exactly this kind of shared low-level piece).
 *
 * Every data surface renders the same triad through them:
 * - loading: skeletons where the shape is stable (never a blocked page);
 * - empty: what is empty, why it might be, what the user can do;
 * - error: human-readable message, retry when safe, request id on
 *   unexpected server failures.
 */
import type { ReactNode } from "react";

import { describeSectionError } from "@/lib/errors";

/** Skeleton block for a list-like section body. */
export function SectionSkeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="section" aria-hidden="true">
      {Array.from({ length: lines }, (_, index) => (
        <span
          key={index}
          className="skeleton skeleton-line"
          data-width={index === lines - 1 ? "narrow" : undefined}
        />
      ))}
    </div>
  );
}

/** Skeleton grid shaped like the task-card list it stands in for. */
export function SectionCardsSkeleton({ cards = 6 }: { cards?: number }) {
  return (
    <div className="skeleton-cards" aria-hidden="true">
      {Array.from({ length: cards }, (_, index) => (
        <div key={index} className="skeleton-card">
          <span className="skeleton skeleton-line" />
          <span className="skeleton skeleton-line" data-width="narrow" />
          <span className="skeleton skeleton-line" />
        </div>
      ))}
    </div>
  );
}

/** Empty state (design §10): what/why/action, centered and quiet. */
export function EmptyState({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <p className="empty-title">{title}</p>
      {hint !== undefined ? <p className="empty-hint">{hint}</p> : null}
      {children}
    </div>
  );
}

/**
 * Section error state: the mapped message (bold marker as a non-color
 * cue), the request id for unexpected failures, and an optional retry
 * control when the load is safely repeatable.
 */
export function SectionError({
  error,
  onRetry,
  retryLabel = "重试",
}: {
  error: unknown;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  const view = describeSectionError(error);
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
      {onRetry !== undefined ? (
        <p>
          <button type="button" className="btn btn-secondary" onClick={onRetry}>
            {retryLabel}
          </button>
        </p>
      ) : null}
    </div>
  );
}

/** Section heading with an optional trailing action (e.g. 查看全部). */
export function SectionHeading({
  title,
  action,
}: {
  title: string;
  action?: ReactNode;
}) {
  return (
    <div className="section-head">
      <h2 className="section-title">{title}</h2>
      {action}
    </div>
  );
}
