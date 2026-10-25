import Link from "next/link";
import { PublicRecruitmentsView } from "@/features/innovation/PublicRecruitmentsView";
import { recruitmentQuery } from "@/features/innovation/recruitmentQuery";
export const metadata = { title: "正在招募 · 华师令" };
export default async function RecruitmentsPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const query = recruitmentQuery(await searchParams);
  return <><div className="page-head"><Link href="/innovation">返回创新创业</Link><h1 className="page-title">正在招募</h1><p className="page-subtitle">校内招募由有资格的项目负责人独立发布，免审。</p></div><PublicRecruitmentsView query={query} /></>;
}
