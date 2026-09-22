/**
 * Primary/secondary submit button with the design-system loading treatment
 * (§9 Buttons, §10 Loading): disabled + inline spinner while the mutation is
 * in flight, `aria-busy` announces the state, and the label stays visible so
 * the button's width and meaning do not shift.
 */
import type { ButtonHTMLAttributes, ReactNode } from "react";

export interface SubmitButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children" | "type"> {
  loading: boolean;
  /** Button label (kept visible while loading). */
  children: ReactNode;
  /** Visual hierarchy; the view must own exactly ONE primary action. */
  variant?: "primary" | "secondary";
  block?: boolean;
}

export function SubmitButton({
  loading,
  children,
  variant = "primary",
  block = true,
  disabled,
  className,
  ...rest
}: SubmitButtonProps) {
  const classes = [
    "btn",
    variant === "primary" ? "btn-primary" : "btn-secondary",
    block ? "btn-block" : "",
    className ?? "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button
      type="submit"
      className={classes}
      disabled={disabled === true || loading}
      aria-busy={loading}
      {...rest}
    >
      {loading ? <span className="spinner" aria-hidden="true" /> : null}
      <span>{children}</span>
    </button>
  );
}
