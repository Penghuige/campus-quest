import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "CampusQuest",
  description: "CampusQuest academic task platform",
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
