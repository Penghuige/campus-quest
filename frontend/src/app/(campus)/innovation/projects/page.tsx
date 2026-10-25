import Link from "next/link";
import { PublicProjectsView } from "@/features/innovation/PublicProjectsView";
import { recruitmentQuery } from "@/features/innovation/recruitmentQuery";
export const metadata = { title: "项目库 · 华师令" };
export default async function ProjectsPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const query = recruitmentQuery(await searchParams);
  return <><div className="page-head"><Link href="/innovation">返回创新创业</Link><h1 className="page-title">项目库</h1><p className="page-subtitle">浏览已核实成果及负责人独立发布的招募项目。</p></div><PublicProjectsView query={query} /></>;
}
