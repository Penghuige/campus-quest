/**
 * Primary/secondary submit button with the design-system loading treatment
 * (§9 Buttons, §10 Loading): disabled + inline spinner while the mutation is
 * in flight, `aria-busy` announces the state, and the label stays visible so
 * the button's width and meaning do not shift.
 *
 * C2-auth: rides the shared Button primitive (class-mapping route) — the
 * variant/block props and the .btn* classes now come from one place; the
 * output className string is byte-identical to the hand-rolled version.
 */
import type { ButtonHTMLAttributes, ReactNode } from "react";

import { Button } from "@/components/ui/button";

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
  return (
    <Button
      type="submit"
      variant={variant}
      block={block}
      disabled={disabled === true || loading}
      aria-busy={loading}
      className={className}
      {...rest}
    >
      {loading ? <span className="spinner" aria-hidden="true" /> : null}
      <span>{children}</span>
    </Button>
  );
}
