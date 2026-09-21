import type { Metadata, Viewport } from "next";

import "./globals.css";

import { DESIGN_TOKENS, oklchToHex } from "@/lib/designTokens";

export const metadata: Metadata = {
  title: "CampusQuest",
  description: "CampusQuest academic task platform",
};

/**
 * Browser chrome color = the shared `--primary` token (converted to sRGB
 * hex; see `lib/designTokens.ts`). The manifest's theme_color covers the
 * installed-app chrome; this covers the tab.
 */
export const viewport: Viewport = {
  themeColor: oklchToHex(DESIGN_TOKENS.primary),
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
