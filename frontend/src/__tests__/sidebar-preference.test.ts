/**
 * Defect #3 (QA 2026-09-30) — sidebar collapse/resize preference: the
 * pure derivations the WorkspaceSidebar resizer renders (persistence
 * parse/serialize, width clamping) plus the token pins that keep the
 * TS pixel constants and the globals.css rem tokens from drifting.
 *
 * The preference is NON-SECRET UI state (a width + a collapsed flag),
 * persisted through the same localStorage conventions as the auth
 * handoff marker: SSR-guarded reads and try/catch writes — private
 * browsing or a full quota must degrade to the defaults, never throw.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, test } from "node:test";

import {
  clampRailWidth,
  parseSidebarPreference,
  RAIL_WIDTH_COLLAPSED_PX,
  RAIL_WIDTH_DEFAULT_PX,
  RAIL_WIDTH_MAX_PX,
  RAIL_WIDTH_MIN_PX,
  readSidebarPreference,
  serializeSidebarPreference,
  SIDEBAR_PREFERENCE_KEY,
  writeSidebarPreference,
} from "../lib/sidebarPreference";

describe("width clamping (the drag pixel range)", () => {
  test("the default equals the CSS token default (14.5rem at 16px root)", () => {
    assert.equal(RAIL_WIDTH_DEFAULT_PX, 232);
  });

  test("the range is sane: min <= default <= max, collapse is below min", () => {
    assert.ok(RAIL_WIDTH_MIN_PX <= RAIL_WIDTH_DEFAULT_PX);
    assert.ok(RAIL_WIDTH_DEFAULT_PX <= RAIL_WIDTH_MAX_PX);
    assert.ok(RAIL_WIDTH_COLLAPSED_PX < RAIL_WIDTH_MIN_PX);
  });

  test("in-range values pass through unchanged", () => {
    assert.equal(clampRailWidth(260), 260);
  });

  test("below-min drags clamp to min, above-max to max", () => {
    assert.equal(clampRailWidth(RAIL_WIDTH_MIN_PX - 40), RAIL_WIDTH_MIN_PX);
    assert.equal(clampRailWidth(RAIL_WIDTH_MAX_PX + 500), RAIL_WIDTH_MAX_PX);
  });

  test("non-finite input falls back to the default, never NaN", () => {
    assert.equal(clampRailWidth(Number.NaN), RAIL_WIDTH_DEFAULT_PX);
    assert.equal(clampRailWidth(Number.POSITIVE_INFINITY), RAIL_WIDTH_MAX_PX);
    assert.equal(clampRailWidth(Number.NEGATIVE_INFINITY), RAIL_WIDTH_MIN_PX);
  });
});

describe("preference parse/serialize roundtrip", () => {
  test("a stored preference roundtrips through serialize -> parse", () => {
    const pref = { collapsed: true, widthPx: 280 };
    assert.deepEqual(
      parseSidebarPreference(serializeSidebarPreference(pref)),
      pref,
    );
  });

  test("an out-of-range stored width is NORMALIZED (clamped), not rejected", () => {
    const parsed = parseSidebarPreference(
      serializeSidebarPreference({ collapsed: false, widthPx: 9999 }),
    );
    assert.deepEqual(parsed, { collapsed: false, widthPx: RAIL_WIDTH_MAX_PX });
  });

  test("structurally invalid payloads return null (caller keeps defaults)", () => {
    assert.equal(parseSidebarPreference(null), null);
    assert.equal(parseSidebarPreference(""), null);
    assert.equal(parseSidebarPreference("not json"), null);
    assert.equal(parseSidebarPreference('{"collapsed":true}'), null);
    assert.equal(parseSidebarPreference('{"collapsed":"yes","widthPx":240}'), null);
    assert.equal(parseSidebarPreference('{"collapsed":true,"widthPx":"wide"}'), null);
    assert.equal(parseSidebarPreference('{"collapsed":true,"widthPx":null}'), null);
  });
});

describe("storage read/write (SSR + private-mode safety)", () => {
  test("readSidebarPreference returns null when storage is unavailable (SSR)", () => {
    assert.equal(readSidebarPreference(undefined), null);
  });

  test("a throwing getItem degrades to null instead of crashing the mount", () => {
    const hostile = {
      getItem() {
        throw new Error("SecurityError: denied");
      },
    };
    assert.equal(readSidebarPreference(hostile), null);
  });

  test("a missing key reads as null; a present key parses", () => {
    const store = new Map<string, string>([
      [SIDEBAR_PREFERENCE_KEY, serializeSidebarPreference({ collapsed: true, widthPx: 248 })],
    ]);
    assert.deepEqual(readSidebarPreference({
      getItem: (k: string) => store.get(k) ?? null,
    }), { collapsed: true, widthPx: 248 });
    const empty = { getItem: () => null };
    assert.equal(readSidebarPreference(empty), null);
  });

  test("a throwing setItem is swallowed (private mode keeps the session usable)", () => {
    const hostile = {
      setItem() {
        throw new Error("QuotaExceededError");
      },
    };
    assert.doesNotThrow(() =>
      writeSidebarPreference(hostile, { collapsed: false, widthPx: 260 }),
    );
  });
});

describe("token pins (TS constants vs globals.css contract)", () => {
  const css = readFileSync(
    join(process.cwd(), "src/app/globals.css"),
    "utf-8",
  );

  test("the storage key stays stable for e2e and future migrations", () => {
    assert.equal(SIDEBAR_PREFERENCE_KEY, "cq:sidebar-preference");
  });

  test("globals.css declares the rail-width tokens the JS mirrors", () => {
    assert.match(css, /--rail-width: 14\.5rem;/);
    assert.match(css, /--rail-width-collapsed: 4\.5rem;/);
  });

  test("the grid column consumes the variable, not a hard-coded width", () => {
    assert.match(css, /grid-template-columns: var\(--rail-width, 14\.5rem\) minmax\(0, 1fr\)/);
    assert.doesNotMatch(css, /grid-template-columns: 14\.5rem minmax\(0, 1fr\)/);
  });

  test("the collapsed constant matches the collapsed token (4.5rem at 16px root)", () => {
    assert.equal(RAIL_WIDTH_COLLAPSED_PX, 72);
  });
});
