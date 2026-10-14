/**
 * First-login rename nudge (V1.1 §6; plan Task 10) — pins for the pure
 * module `features/auth/nicknameNudge.ts`:
 *
 * - the trigger predicate recognises exactly the AUTO-GENERATED
 *   nickname shapes (all digits — student numbers and digit local
 *   parts; equal to the username's local part — staff emails): a real
 *   chosen name never triggers, and the predicate flips false the
 *   moment the account is renamed (the "never re-shown after rename"
 *   rule is predicate-driven, not storage-driven);
 * - dismissal persists per-account in localStorage under
 *   `cq-nudge-rename:{user_id}` and survives only through the injected
 *   StorageLike (the component wraps it in the safe-localStorage
 *   pattern; a throw/deny just reads as "not dismissed").
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  isAutoNickname,
  nudgeStorageKey,
  readNudgeDismissed,
  writeNudgeDismissed,
  type NudgeStorage,
} from "../features/auth/nicknameNudge";

/** Minimal Storage fake (get/set only — the module's whole contract). */
function fakeStorage(): NudgeStorage {
  const backing = new Map<string, string>();
  return {
    getItem: (key: string) => backing.get(key) ?? null,
    setItem: (key: string, value: string) => {
      backing.set(key, value);
    },
  };
}

describe("isAutoNickname (the auto-generated shapes, V1.1 §6)", () => {
  test("all-digits nicknames trigger (student numbers, digit local parts)", () => {
    assert.equal(isAutoNickname("20260001", "20260001"), true);
    assert.equal(isAutoNickname("1234567890", "1234567890@qq.com"), true);
    assert.equal(isAutoNickname("0", "whatever"), true);
  });

  test("nickname equal to the username's local part triggers (staff email)", () => {
    assert.equal(isAutoNickname("owner.name", "owner.name@school.edu"), true);
  });

  test("a chosen name never triggers; mixed content never triggers", () => {
    assert.equal(isAutoNickname("小明", "20260001"), false);
    assert.equal(isAutoNickname("小明20260001", "20260001"), false);
    assert.equal(isAutoNickname("Teacher Wang", "twang@school.edu"), false);
  });

  test("rename flips the predicate off even while the dismissal key is unset", () => {
    assert.equal(isAutoNickname("小明", "1234567890@qq.com"), false);
  });
});

describe("dismiss persistence (cq-nudge-rename:{user_id})", () => {
  test("the key embeds the user id; different accounts are independent", () => {
    assert.equal(nudgeStorageKey("u-1"), "cq-nudge-rename:u-1");
    assert.notEqual(nudgeStorageKey("u-1"), nudgeStorageKey("u-2"));
  });

  test("undismissed reads false; a write persists; storage is honored verbatim", () => {
    const storage = fakeStorage();
    assert.equal(readNudgeDismissed(storage, "u-1"), false);
    writeNudgeDismissed(storage, "u-1");
    assert.equal(readNudgeDismissed(storage, "u-1"), true);
    assert.equal(readNudgeDismissed(storage, "u-2"), false);
  });
});
