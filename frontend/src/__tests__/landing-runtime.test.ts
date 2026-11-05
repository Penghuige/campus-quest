import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

const root = resolve("public/landing");
test("production Landing ships its complete local runtime with matching bytes", () => {
  assert.ok(existsSync(`${root}/runtime-manifest.json`), "production runtime manifest is present");
  const manifest = JSON.parse(readFileSync(`${root}/runtime-manifest.json`, "utf8"));
  assert.equal(manifest.files.length, 81);
  for (const item of manifest.files) {
    assert.ok(!item.path.includes("..") && !item.path.startsWith("/"));
    const bytes = readFileSync(resolve(root, item.path));
    assert.equal(createHash("sha256").update(bytes).digest("hex"), item.sha256, item.path);
  }
});
test("real login targets retain deployment prefixes and reject non-web origins", async () => {
  assert.ok(existsSync(`${root}/navigation.mjs`), "production navigation module exists");
  const { getLoginURL } = await import(pathToFileURL(`${root}/navigation.mjs`).href);
  assert.equal(getLoginURL("https://campus.test/landing/navigation.mjs"), "https://campus.test/login");
  assert.equal(getLoginURL("https://campus.test/cq/landing/navigation.mjs"), "https://campus.test/cq/login");
  assert.throws(() => getLoginURL("file:///tmp/landing/navigation.mjs"));
});
test("both visible entries escape the frame and no production demo or tools remain", () => {
  assert.ok(existsSync(`${root}/index.html`), "production HTML exists");
  const html = readFileSync(`${root}/index.html`, "utf8");
  const entry = readFileSync(`${root}/hero/entry.mjs`, "utf8");
  const render = readFileSync(`${root}/seal/render.mjs`, "utf8");
  assert.match(html, /class="direct-entry"[^>]*target="_top"/);
  assert.match(entry, /entry\.target='_top'/);
  assert.match(render, /navigateToLogin/);
  assert.doesNotMatch(html + entry + render, /演示|<details id="tools">|\?inspect=1/);
  assert.match(html, /<noscript>/);
});
