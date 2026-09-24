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
      <head>
        {/* Theme shoot-out switch (Plan 11 direction pass): `?theme=ink|
            cream|scale` opts the page into a CSS token variant; absent
            param = the merged default look. Runs before paint so there
            is no flash; pure presentation, removed or folded into the
            chosen direction when the shoot-out concludes. */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "try{var t=new URLSearchParams(location.search).get('theme');if(t){document.documentElement.dataset.theme=t}}catch(e){}",
          }}
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
