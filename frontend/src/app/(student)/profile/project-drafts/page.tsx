import type { Metadata } from "next";
import Link from "next/link";

import { ProjectDraftsView } from "@/features/innovation/ProjectDraftsView";

export const metadata: Metadata = {
  title: "我的项目草稿 · CampusQuest",
  description: "准备仅自己可见的创新创业项目概况",
};

export default function ProjectDraftsPage() {
  return (
    <>
      <div className="page-head">
        <Link className="section-link" href="/profile">返回我的档案</Link>
        <h1 className="page-title">我的项目草稿</h1>
        <p className="page-subtitle">仅自己可见。先记录项目概况，未完成的内容可以稍后补充。</p>
      </div>
      <ProjectDraftsView />
    </>
  );
}
