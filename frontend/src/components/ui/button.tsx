"use client";
/**
 * Plan-12 Phase C / C2-student: shadcn-pattern Button primitive.
 *
 * Class-mapping ruling (the plan-14 T4 "pick one and record it"
 * discipline): the primitive OWNS the mapping from semantic variant to
 * the existing `.btn*` classes in globals.css — the visual shell gains
 * NO new CSS, the five context overrides that style `.btn` descendants
 * (`.otp-row` / `.avatar-actions` / `.dialog-actions` / `.row-actions`
 * / `.cq-dialog-footer`) keep applying verbatim, so the C3
 * retirement could re-point every button in the product by changing
 * the mapping in THIS file alone. (The alternative — a parallel
 * `.cq-btn*` family token-copied from `.btn` — would have
 * duplicated five context overrides and doubled the drift surface
 * before C3.)
 *
 * `type` defaults to "button" (explicit-safe); form submit sites pass
 * type="submit" — adoption audited every call site so no implicit
 * submit survives the primitive. asChild rides @radix-ui/react-slot
 * (Link-as-button sites keep their anchor semantics).
 */
import * as React from "react";
import { Slot } from "@radix-ui/react-slot";

import { cn } from "@/lib/utils";

export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary: "btn-primary",
  secondary: "btn-secondary",
  danger: "btn-danger",
  ghost: "btn-ghost",
};

export interface ButtonProps extends React.ComponentProps<"button"> {
  variant?: ButtonVariant;
  /** Full-width (the `.btn-block` modifier). */
  block?: boolean;
  /** Render the child element instead of a <button> (Link-as-button). */
  asChild?: boolean;
}

function Button({
  className,
  variant = "primary",
  block = false,
  type,
  asChild = false,
  ...props
}: ButtonProps) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp
      data-slot="button"
      type={asChild ? undefined : (type ?? "button")}
      className={cn("btn", VARIANT_CLASSES[variant], block && "btn-block", className)}
      {...props}
    />
  );
}

export { Button };
