import Link from "next/link";
import { RecruitmentWorkspace } from "@/features/innovation/RecruitmentWorkspace";
export const metadata = { title: "我的招募 · 华师令" };
export default async function MyRecruitmentsPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  return <><div className="page-head"><Link href="/profile/project-drafts">返回我的项目</Link><h1 className="page-title">我的招募</h1><p className="page-subtitle">草稿、校内发布与联系授权在这里管理。</p></div><RecruitmentWorkspace projectId={projectId} /></>;
}
