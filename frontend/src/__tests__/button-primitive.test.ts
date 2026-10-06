/**
 * C2-student: shadcn-pattern Button primitive — the class-mapping pin.
 *
 * Same technique as dialog-primitive.test.ts: Button is a plain
 * function component, so calling it returns the React element
 * descriptor — `type` proves the button/Slot binding, `props` proves
 * the variant→class mapping (the ruling this file exists to freeze:
 * the primitive OWNS the mapping; C3 re-points it here alone),
 * the explicit-safe `type` default, and the asChild passthrough.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { Slot } from "@radix-ui/react-slot";

import { Button } from "../components/ui/button";

test("variants map onto the existing .btn* classes (no new CSS)", () => {
  assert.equal(Button({ variant: "primary" }).props.className, "btn btn-primary");
  assert.equal(Button({ variant: "secondary" }).props.className, "btn btn-secondary");
  assert.equal(Button({ variant: "danger" }).props.className, "btn btn-danger");
  assert.equal(Button({ variant: "ghost" }).props.className, "btn btn-ghost");
});

test("primary is the default variant; block adds the modifier", () => {
  assert.equal(Button({}).props.className, "btn btn-primary");
  assert.equal(Button({ variant: "secondary", block: true }).props.className, "btn btn-secondary btn-block");
});

test("caller className composes AFTER the mapping (context hooks win)", () => {
  assert.equal(
    Button({ variant: "ghost", className: "comment-action" }).props.className,
    "btn btn-ghost comment-action",
  );
});

test("type defaults to button (explicit-safe); submit is opt-in", () => {
  assert.equal(Button({}).props.type, "button");
  assert.equal(Button({ type: "submit" }).props.type, "submit");
});

test("asChild swaps to the Radix Slot and drops the button-only type", () => {
  const element = Button({ asChild: true, type: "submit" });
  assert.equal(element.type, Slot);
  assert.equal(element.props.type, undefined);
  assert.equal(element.props.className, "btn btn-primary");
});

test("native props and data-slot ride through untouched", () => {
  const element = Button({
    variant: "secondary",
    disabled: true,
    "aria-label": "重试上传",
    onClick: () => undefined,
  });
  assert.equal(element.props["data-slot"], "button");
  assert.equal(element.props.disabled, true);
  assert.equal(element.props["aria-label"], "重试上传");
  assert.equal(typeof element.props.onClick, "function");
});
