/**
 * The recent-logins ledger (batch 2, 2026-10-20): the login page's
 * account-quick-switch cards. Pins the whole module contract:
 * - the MASK shapes (G11 — the card never shows a full phone/email);
 * - upsert-by-identifier with a lastUsedAt bump, newest-first listing;
 * - the 5-entry cap (the OLDEST rides out);
 * - removal by identifier (the card's 从本机移除 verb);
 * - dirty-data defense: malformed rows are filtered, never thrown;
 * - storage-absence tolerance (private mode / node): no call throws.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import {
  listRecentLogins,
  maskIdentifier,
  recordRecentLogin,
  removeRecentLogin,
} from "../features/auth/recentLogins";

/** A minimal Storage stub (node has no localStorage global). */
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

const base = {
  identifier: "13900001234",
  masked: "+86****1234",
  role: "STUDENT" as const,
  nickname: "小明",
  lastUsedAt: 1000,
};

beforeEach(() => {
  (globalThis as { localStorage?: Storage }).localStorage = stubStorage();
});

afterEach(() => {
  delete (globalThis as { localStorage?: Storage }).localStorage;
});

describe("maskIdentifier (G11 — the card never shows the full address)", () => {
  test("a CN mobile masks the middle", () => {
    assert.equal(maskIdentifier("13900001234"), "+86****1234");
  });

  test("an email keeps the first local char and the domain", () => {
    assert.equal(maskIdentifier("teacher@school.edu"), "t***@school.edu");
  });

  test("a student number masks to its bookends (the card never shows the whole)", () => {
    assert.equal(maskIdentifier("20240001"), "20****01");
    assert.equal(maskIdentifier("123456"), "12****56");
  });

  test("an E.164 form masks like the bare CN mobile (the backend's mask_phone shape)", () => {
    // The probe hands E.164; a card's identifier may be stored in it
    // too — an unmasked passthrough would leak the whole number (G11)
    // AND never match the probe's mask. Every recognisable mobile form
    // normalizes to the SAME +86****tail shape.
    assert.equal(maskIdentifier("+8613812345678"), "+86****5678");
    assert.equal(maskIdentifier("+86 13812345678"), "+86****5678");
    assert.equal(maskIdentifier("8613812345678"), "+86****5678");
  });
});

describe("recordRecentLogin / listRecentLogins", () => {
  test("records and lists newest-first with the bump on re-login", () => {
    recordRecentLogin({ ...base });
    recordRecentLogin({ ...base, identifier: "teacher@school.edu", masked: "t***@school.edu", role: "TEACHER", nickname: "T", lastUsedAt: 2000 });
    let list = listRecentLogins();
    assert.equal(list.length, 2);
    assert.equal(list[0].identifier, "teacher@school.edu"); // newer first

    // Re-login the FIRST entry: it bumps to the head.
    recordRecentLogin({ ...base, lastUsedAt: 3000 });
    list = listRecentLogins();
    assert.equal(list[0].identifier, "13900001234");
    assert.equal(list.length, 2); // upsert, not append
  });

  test("the cap is FIVE — the oldest rides out", () => {
    for (let i = 0; i < 7; i += 1) {
      recordRecentLogin({
        ...base,
        identifier: `user${i}@school.edu`,
        masked: `u${i}***@school.edu`,
        lastUsedAt: 1000 + i,
      });
    }
    const list = listRecentLogins();
    assert.equal(list.length, 5);
    assert.equal(list[0].identifier, "user6@school.edu"); // newest
    assert.equal(list[4].identifier, "user2@school.edu"); // user0/user1 evicted
  });
});

describe("removeRecentLogin (the card's 从本机移除 verb)", () => {
  test("removes exactly the one row, keeps the neighbours", () => {
    recordRecentLogin({ ...base });
    recordRecentLogin({ ...base, identifier: "teacher@school.edu", masked: "t***@school.edu", role: "TEACHER", nickname: "T", lastUsedAt: 2000 });
    removeRecentLogin("13900001234");
    const list = listRecentLogins();
    assert.equal(list.length, 1);
    assert.equal(list[0].identifier, "teacher@school.edu");
  });
});

describe("dirty-data defense (a corrupted jar never throws)", () => {
  test("malformed rows are filtered out of the listing", () => {
    (globalThis as { localStorage?: Storage }).localStorage = stubStorage({
      "cq.recent-logins": JSON.stringify([
        { identifier: "13900001234", masked: "+86****1234", role: "STUDENT", nickname: "小明", lastUsedAt: 1 },
        { garbage: true },
        "not-an-object",
        { identifier: 42, masked: null, role: "WIZARD", nickname: "", lastUsedAt: "x" },
      ]),
    });
    const list = listRecentLogins();
    assert.equal(list.length, 1);
    assert.equal(list[0].identifier, "13900001234");
  });

  test("no localStorage at all: every call still answers (empty/silent)", () => {
    delete (globalThis as { localStorage?: Storage }).localStorage;
    assert.deepEqual(listRecentLogins(), []);
    recordRecentLogin({ ...base }); // must not throw
    removeRecentLogin("13900001234"); // must not throw
    assert.deepEqual(listRecentLogins(), []);
  });
});

describe("the card's device_slot (the slot-matching contract, 2026-10-24)", () => {
  test("a minted login writes its slot onto the card", () => {
    recordRecentLogin({ ...base, device_slot: "a1b2c3d4e5" });
    assert.equal(listRecentLogins()[0].device_slot, "a1b2c3d4e5");
  });

  test("ABSENT never overwrites — a re-login without the checkbox keeps the older, still-live slot", () => {
    recordRecentLogin({ ...base, device_slot: "a1b2c3d4e5" });
    // The mint-only rule: an unminted login's response OMITS the field;
    // the card keeps the old slot (revoking it is revoke_all's job).
    recordRecentLogin({ ...base, lastUsedAt: 2000 });
    const after = listRecentLogins()[0];
    assert.equal(after.device_slot, "a1b2c3d4e5");
    assert.equal(after.lastUsedAt, 2000); // the upsert still bumps recency
  });

  test("a re-mint REPLACES the slot (the account's handle moved)", () => {
    recordRecentLogin({ ...base, device_slot: "a1b2c3d4e5" });
    recordRecentLogin({ ...base, device_slot: "f6g7h8i9j0" });
    assert.equal(listRecentLogins()[0].device_slot, "f6g7h8i9j0");
  });

  test("legacy rows without the field stay valid (one-time code-flow degradation, not a drop)", () => {
    (globalThis as { localStorage?: Storage }).localStorage = stubStorage({
      "cq.recent-logins": JSON.stringify([
        { identifier: "teacher@school.edu", masked: "t***@school.edu", role: "TEACHER", nickname: "", lastUsedAt: 1 },
      ]),
    });
    const list = listRecentLogins();
    assert.equal(list.length, 1);
    assert.equal(list[0].device_slot, undefined);
  });
});
