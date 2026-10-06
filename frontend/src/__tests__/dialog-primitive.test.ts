/**
 * Plan-14 T4: shadcn-pattern Dialog primitive — composition / props-mapping
 * tests (no DOM shim in this repo's tsx --test runner; live behavior —
 * role=dialog, aria-modal, focus trap, Escape — is proven by the T5-T8
 * batch e2e, per the task brief).
 *
 * Technique: the primitive parts are plain function components with no
 * hooks, so calling them directly returns the React element descriptor —
 * `type` proves the Radix binding, `props` proves class/data-slot wiring
 * and prop passthrough.
 */
import assert from "node:assert/strict";
import test from "node:test";
import type { ReactElement, ReactNode } from "react";

import * as DialogPrimitive from "@radix-ui/react-dialog";

import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogOverlay,
  DialogTitle,
  DialogTrigger,
} from "../components/ui/dialog";
import { cn } from "../lib/utils";

function childrenOf(el: ReactElement): ReactElement[] {
  const kids = (el.props as { children?: ReactNode }).children;
  return (Array.isArray(kids) ? kids : [kids]) as ReactElement[];
}

test("cn joins conditional classes and resolves tailwind-merge conflicts", () => {
  assert.equal(cn("cq-dialog-content", false && "cq-dialog-wide"), "cq-dialog-content");
  assert.equal(cn("cq-dialog-content", "extra"), "cq-dialog-content extra");
  // tailwind-merge is really wired: later conflicting utility wins.
  assert.equal(cn("px-2", "px-4"), "px-4");
});

test("Dialog and DialogTrigger bind the Radix Root/Trigger with data-slot", () => {
  const root = Dialog({ open: true, children: null });
  assert.equal(root.type, DialogPrimitive.Root);
  assert.equal((root.props as Record<string, unknown>)["data-slot"], "dialog");
  assert.equal((root.props as Record<string, unknown>).open, true);

  const trigger = DialogTrigger({ children: "打开" });
  assert.equal(trigger.type, DialogPrimitive.Trigger);
  assert.equal((trigger.props as Record<string, unknown>)["data-slot"], "dialog-trigger");
});

test("DialogClose binds Radix Close (asChild passthrough for action rows)", () => {
  const close = DialogClose({ asChild: true, children: null });
  assert.equal(close.type, DialogPrimitive.Close);
  assert.equal((close.props as Record<string, unknown>).asChild, true);
});

test("DialogContent maps to Portal > Overlay + Radix Content, CampusQuest classes", () => {
  const overlayEl = DialogOverlay({ className: "scrim-x" });
  assert.equal(overlayEl.type, DialogPrimitive.Overlay);
  assert.equal(
    (overlayEl.props as Record<string, unknown>).className,
    "cq-dialog-overlay scrim-x",
  );
  assert.equal((overlayEl.props as Record<string, unknown>)["data-slot"], "dialog-overlay");

  const portal = DialogContent({ "aria-labelledby": "redeem-dialog-title", children: "body" });
  assert.equal(portal.type, DialogPrimitive.Portal);

  const [overlay, content] = childrenOf(portal);
  assert.equal(overlay.type, DialogOverlay);

  assert.equal(content.type, DialogPrimitive.Content);
  const props = content.props as Record<string, unknown>;
  assert.equal(props.className, "cq-dialog-content");
  assert.equal(props["data-slot"], "dialog-content");
  // Class A passthrough: the labelledby id must reach the content element.
  assert.equal(props["aria-labelledby"], "redeem-dialog-title");
  assert.equal(props.children, "body");
});

test("DialogContent size=\"wide\" adds cq-dialog-wide; caller className last", () => {
  const portal = DialogContent({ size: "wide", className: "extra", children: null });
  const [, content] = childrenOf(portal);
  assert.equal(
    (content.props as Record<string, unknown>).className,
    "cq-dialog-content cq-dialog-wide extra",
  );
});

test("DialogTitle/Description/Footer map Radix parts to .cq-dialog* classes", () => {
  const title = DialogTitle({ id: "redeem-dialog-title", children: "确认兑换" });
  assert.equal(title.type, DialogPrimitive.Title);
  assert.equal((title.props as Record<string, unknown>).className, "cq-dialog-title");
  assert.equal((title.props as Record<string, unknown>).id, "redeem-dialog-title");

  const description = DialogDescription({ children: "hint" });
  assert.equal(description.type, DialogPrimitive.Description);
  assert.equal(
    (description.props as Record<string, unknown>).className,
    "cq-dialog-description",
  );

  const footer = DialogFooter({ className: "x", children: null });
  assert.equal(footer.type, "div");
  assert.equal((footer.props as Record<string, unknown>).className, "cq-dialog-footer x");
});
