import type { Metadata } from "next";

import { AccountSettings } from "@/features/auth/AccountSettings";
import { GrowthView } from "@/features/rankings/GrowthView";

export const metadata: Metadata = {
  title: "我的档案 · CampusQuest",
  description: "成长数据、获得的荣誉与账号设置",
};

/**
 * Profile page (spec §19/§42): the growth island (metrics + honors)
 * above the account-settings island. Both are session-gated by the
 * layout; each section owns its loading/empty/error states.
 */
export default function ProfilePage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">我的档案</h1>
        <p className="page-subtitle">成长数据、获得的荣誉与账号设置</p>
      </div>
      <GrowthView />
      <AccountSettings />
    </>
  );
}
