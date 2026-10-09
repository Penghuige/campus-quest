import type { Metadata } from "next";
import Link from "next/link";

import { AvatarSection } from "@/features/auth/AvatarSection";
import { AccountSettings } from "@/features/auth/AccountSettings";
import { GrowthView } from "@/features/rankings/GrowthView";
import { InnovationCapabilities } from "@/features/innovation/InnovationCapabilities";
import {
  DEFAULT_PROFILE_TAB,
  parseProfileTab,
  PROFILE_TABS,
} from "@/features/profile/profileTabs";

export const metadata: Metadata = {
  title: "我的档案 · CampusQuest",
  description: "成长数据、获得的荣誉与个人信息",
};

interface ProfilePageProps {
  searchParams: Promise<{ tab?: string | string[] }>;
}

/**
 * Profile page (spec §19/§42; defect #4 QA 2026-09-30): two URL-state
 * tabs (patterns §4 — the rankings ?period= precedent). 我的档案 keeps
 * the landing slot; the 个人信息 subpage now owns ALL account editing
 * (nickname/avatar/phone/email/password — formerly interleaved with
 * the growth data). The server parses the tab (garbage degrades to
 * the default); the avatar section gates itself on the live
 * has_avatar contract (absent until the backend avatar PR deploys).
 */
export default async function ProfilePage({ searchParams }: ProfilePageProps) {
  const params = await searchParams;
  const raw = params.tab;
  const tab = parseProfileTab(Array.isArray(raw) ? raw[0] : raw);

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">我的档案</h1>
        <p className="page-subtitle">成长数据、获得的荣誉与个人信息</p>
        <Link className="section-link" href="/profile/project-drafts">我的项目草稿</Link>
        <Link className="section-link" href="/profile/owner-profile">负责人资料</Link>
        <Link className="section-link" href="/innovation/achievements">浏览校内成果</Link>
      </div>
      <nav className="tab-bar" aria-label="个人主页分区">
        {PROFILE_TABS.map((item) => (
          <Link
            key={item.key}
            href={item.key === DEFAULT_PROFILE_TAB ? "/profile" : `/profile?tab=${item.key}`}
            className="tab-link"
            aria-current={item.key === tab ? "page" : undefined}
          >
            {item.label}
          </Link>
        ))}
      </nav>
      {tab === "growth" ? (
        <><InnovationCapabilities /><GrowthView /></>
      ) : (
        <>
          <AvatarSection />
          <AccountSettings />
        </>
      )}
    </>
  );
}
