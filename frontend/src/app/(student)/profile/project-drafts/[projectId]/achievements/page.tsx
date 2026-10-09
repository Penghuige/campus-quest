import Link from "next/link";
import { AchievementDraftsView } from "@/features/innovation/AchievementDraftsView";

export const metadata = { title: "我的成果草稿" };

export default async function AchievementDraftsPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  return <>
    <div className="page-head"><Link className="section-link" href="/profile/project-drafts">返回项目草稿</Link><h1 className="page-title">我的成果草稿</h1><p className="page-subtitle">在项目下整理作品与成果，准备好后再进入后续核实流程。</p></div>
    <AchievementDraftsView projectId={projectId} />
  </>;
}
