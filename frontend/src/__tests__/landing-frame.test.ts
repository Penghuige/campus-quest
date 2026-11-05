import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

test("public introduction isolates the document and retains a no-script login path", async () => {
  assert.ok(existsSync("src/features/landing/LandingPage.tsx"), "Landing page component exists");
  const { LandingPage } = await import("../features/landing/LandingPage");
  const html = renderToStaticMarkup(createElement(LandingPage));
  assert.match(html, /title="智汇华师校园介绍"/);
  assert.match(html, /src="\/landing\/index.html"/);
  assert.match(html, /<noscript>/);
  assert.match(html, /href="\/login"/);
});
