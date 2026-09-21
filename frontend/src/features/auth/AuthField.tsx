/**
 * Labeled input field for the auth forms (design system §9 Forms, §12):
 * label stays visible, hint explains consequences, error text sits next to
 * the field, and the association is programmatic (`htmlFor`/`id`,
 * `aria-invalid`, `aria-describedby`). Non-color cues: the error line is
 * text; `aria-invalid` also drives the border color as a supplement.
 */
import type { ReactNode } from "react";

export interface AuthFieldProps {
  /** Form-unique field name; also the input id. */
  name: string;
  label: string;
  error?: string | null;
  /** Help text (consequences, not the field name; §9). */
  hint?: ReactNode;
  /** Right-aligned counter, e.g. `12/16` (tabular numerals). */
  counter?: string | null;
  /** Flags the counter as over-limit for supplemental styling. */
  counterOver?: boolean;
  inputProps: Omit<
    React.ComponentPropsWithRef<"input">,
    "id" | "name" | "aria-invalid" | "aria-describedby" | "className"
  >;
}

export function AuthField({
  name,
  label,
  error,
  hint,
  counter,
  counterOver,
  inputProps,
}: AuthFieldProps) {
  const errorId = `${name}-error`;
  const hintId = `${name}-hint`;
  const describedBy =
    [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(" ") ||
    undefined;

  return (
    <div className="field">
      <div className="field-head">
        <label className="field-label" htmlFor={name}>
          {label}
        </label>
        {counter !== undefined && counter !== null ? (
          <span className="field-counter" data-over={counterOver ? "true" : undefined}>
            {counter}
          </span>
        ) : null}
      </div>
      <input
        className="input"
        id={name}
        name={name}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        {...inputProps}
      />
      {hint ? (
        <p className="field-hint" id={hintId}>
          {hint}
        </p>
      ) : null}
      {error ? (
        <p className="field-error" id={errorId}>
          {error}
        </p>
      ) : null}
    </div>
  );
}
