/**
 * PWA web app manifest (brief step 2; Next 16 `app/manifest.ts` file
 * convention — the metadata API links it as `/manifest.webmanifest`
 * automatically, no manual link tag anywhere).
 *
 * Token-first colors (design-system §3): `theme_color` and
 * `background_color` are CONVERTED FROM the shared OKLCH design tokens via
 * `oklchToHex` — manifests are parsed as sRGB before any page CSS exists,
 * so raw `oklch()` strings would be unreliably supported there. The unit
 * test pins both the token strings (against globals.css) and the resulting
 * hex values.
 *
 * Icons are project-owned PNGs under `public/icons/`, generated from the
 * same token color by `scripts/generate-pwa-icons.py` (Pillow, no external
 * asset dependency); see that script for the exact recipe. V1 ships
 * installability only — NO push notifications (out of scope per the plan).
 */
import type { MetadataRoute } from "next";

import { DESIGN_TOKENS, oklchToHex } from "@/lib/designTokens";

export default function manifest(): MetadataRoute.Manifest {
  return {
    id: "/",
    name: "CampusQuest",
    short_name: "CampusQuest",
    description: "校园任务平台：领取任务、累积积分、兑换奖励",
    lang: "zh-CN",
    start_url: "/",
    display: "standalone",
    background_color: oklchToHex(DESIGN_TOKENS.background),
    theme_color: oklchToHex(DESIGN_TOKENS.primary),
    icons: [
      {
        src: "/icons/icon-192.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icons/icon-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "any",
      },
      {
        src: "/icons/icon-maskable-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
  };
}
