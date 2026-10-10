"use client";
/**
 * Plan-14 T4: shadcn-pattern Dialog primitive on @radix-ui/react-dialog,
 * styled to the CampusQuest dialog contract.
 *
 * Styling ruling (plan-14 T4): this repo compiles NO default Tailwind
 * scales (utilities-only import, no default theme — `p-4`, `md:*`,
 * `rounded-lg` generate nothing), so the visual shell is plain CSS
 * classes `.cq-dialog*` in globals.css (values inherited from the
 * retired native `.dialog` contract, C3):
 * width min(26rem, 100vw - 2*--space-4) / 40rem wide variant, --border,
 * --radius-lg, --shadow-dialog, --surface-1, --overlay-scrim scrim, and
 * NO open/close animation (the product has none).
 *
 * Radix supplies the behavior contract (selector contract Class A):
 * role="dialog", aria-labelledby/aria-describedby wiring, focus trap,
 * Escape, outside-click close. Modality is enforced by aria-hiding
 * sibling content (hideOthers) + scroll lock; @radix-ui/react-dialog
 * 1.2 does not emit aria-modal. There is intentionally no built-in
 * close button — the product dialogs close through their own
 * action rows (use DialogClose asChild on those buttons).
 */
import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";

import { cn } from "@/lib/utils";

function Dialog({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Root>) {
  return <DialogPrimitive.Root data-slot="dialog" {...props} />;
}

function DialogTrigger({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Trigger>) {
  return <DialogPrimitive.Trigger data-slot="dialog-trigger" {...props} />;
}

function DialogClose({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Close>) {
  return <DialogPrimitive.Close data-slot="dialog-close" {...props} />;
}

function DialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="dialog-overlay"
      className={cn("cq-dialog-overlay", className)}
      {...props}
    />
  );
}

function DialogContent({
  className,
  children,
  size = "default",
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & {
  /** "default" = 26rem card; "wide" = 40rem (the retired .dialog
   *  contract's widths, carried by .cq-dialog-content/.cq-dialog-wide). */
  size?: "default" | "wide";
}) {
  return (
    <DialogPrimitive.Portal>
      <DialogOverlay />
      <DialogPrimitive.Content
        data-slot="dialog-content"
        className={cn(
          "cq-dialog-content",
          size === "wide" && "cq-dialog-wide",
          className,
        )}
        {...props}
      >
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

function DialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      data-slot="dialog-title"
      className={cn("cq-dialog-title", className)}
      {...props}
    />
  );
}

function DialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      data-slot="dialog-description"
      className={cn("cq-dialog-description", className)}
      {...props}
    />
  );
}

function DialogFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn("cq-dialog-footer", className)}
      {...props}
    />
  );
}

export {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogFooter,
  DialogClose,
  DialogOverlay,
};
