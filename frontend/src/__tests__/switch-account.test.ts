/**
 * The switch-account handoff (popover batch, owner feedback
 * 2026-10-23): the shells' 切换账号 menu model. Pins:
 * - the menu rows: the ledger minus its HEAD (the current login —
 *   every success path funnels through the panel's recordLogin, which
 *   upserts the used identifier to the head), capped at four;
 * - the empty shape: a single-account device answers [] (the button's
 *   direct-hop fallback);
 * - setSwitchPick/takeSwitchPick: the sessionStorage handoff — read
 *   ONCE (the read deletes), tolerant of a missing or blocked store.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  SWITCH_PICK_KEY,
  setSwitchPick,
  switchMenuEntries,
  takeSwitchPick,
} from "../features/auth/switchAccount";
import type { RecentLoginEntry } from "../features/auth/recentLogins";

/** A minimal Storage stub (node has no sessionStorage global). */
function stubStorage(initial: Record<string, string> = {}): Storage {
  const store = new Map(Object.entries(initial));
  return {
    get length() {
      return store.size;
    },
    clear() {
      store.clear();
    },
    getItem(key) {
      return store.get(key) ?? null;
    },
    key(index) {
      return [...store.keys()][index] ?? null;
    },
    removeItem(key) {
      store.delete(key);
    },
    setItem(key, value) {
      store.set(key, value);
    },
  };
}

function entry(identifier: string, lastUsedAt: number): RecentLoginEntry {
  return { identifier, masked: identifier, role: "STUDENT", nickname: "", lastUsedAt };
}

beforeEach(() => {
  (globalThis as { sessionStorage?: Storage }).sessionStorage = stubStorage();
});

afterEach(() => {
  delete (globalThis as { sessionStorage?: Storage }).sessionStorage;
});

describe("switchMenuEntries (the menu rows: ledger minus the current login)", () => {
  test("the head (current account) is excluded; the rest keep order", () => {
    const rows = [entry("a", 40), entry("b", 30), entry("c", 20)];
    assert.deepEqual(
      switchMenuEntries(rows).map((row) => row.identifier),
      ["b", "c"],
    );
  });

  test("caps at four even against a full five-row jar", () => {
    const rows = ["a", "b", "c", "d", "e"].map((id, index) => entry(id, 50 - index));
    assert.deepEqual(
      switchMenuEntries(rows).map((row) => row.identifier),
      ["b", "c", "d", "e"],
    );
  });

  test("a single-account device answers empty (the direct-hop fallback)", () => {
    assert.deepEqual(switchMenuEntries([entry("only", 10)]), []);
    assert.deepEqual(switchMenuEntries([]), []);
  });
});

describe("the switch-pick handoff (sessionStorage, read-once)", () => {
  test("the key name is the contract", () => {
    assert.equal(SWITCH_PICK_KEY, "cq.switch-pick");
  });

  test("set then take answers once and deletes", () => {
    setSwitchPick("teacher@school.edu");
    assert.equal(takeSwitchPick(), "teacher@school.edu");
    assert.equal(takeSwitchPick(), null);
  });

  test("take on an empty store answers null and never throws", () => {
    assert.equal(takeSwitchPick(), null);
  });

  test("a blocked store is a no-op, never an error", () => {
    delete (globalThis as { sessionStorage?: Storage }).sessionStorage;
    setSwitchPick("teacher@school.edu");
    assert.equal(takeSwitchPick(), null);
  });
});
