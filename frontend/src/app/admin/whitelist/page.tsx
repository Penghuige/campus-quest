import type { Metadata } from "next";

import { WhitelistAdmin } from "@/features/admin/WhitelistAdmin";

export const metadata: Metadata = {
  title: "注册白名单 · CampusQuest 管理后台",
  description: "学生注册白名单的批量导入（预览 / 确认）与条目启停",
};

/**
 * Admin whitelist page (spec §5.1): the preview/confirm import island
 * plus the per-entry enable/disable listing.
 */
export default function AdminWhitelistPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">注册白名单</h1>
        <p className="page-subtitle">
          批量导入先预览逐行校验结果，确认后一次性写入；冲突时整体不写入
        </p>
      </div>
      <WhitelistAdmin />
    </>
  );
}
