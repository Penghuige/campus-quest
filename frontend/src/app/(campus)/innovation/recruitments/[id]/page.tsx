import { PublicRecruitmentsView } from "@/features/innovation/PublicRecruitmentsView";
export const metadata = { title: "招募岗位 · 华师令" };
export default async function RecruitmentPage({ params }: { params: Promise<{ id: string }> }) {
  return <PublicRecruitmentsView id={(await params).id} />;
}
