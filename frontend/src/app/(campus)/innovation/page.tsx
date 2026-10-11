import type { Metadata } from "next";
import Link from "next/link";
import { PublicAchievementsView } from "@/features/innovation/PublicAchievementsView";
import { PublicRecruitmentsView } from "@/features/innovation/PublicRecruitmentsView";

export const metadata: Metadata = { title: "创新创业 · 华师令" };

export default function InnovationPage() {
  return <>
    <div className="page-head">
      <h1 className="page-title">创新创业</h1>
      <p className="page-subtitle">浏览校内成果与招募，了解项目与实践方向</p>
      <p className="field-hint">演示阶段：使用预置账号；演示资料为模拟内容。学校统一身份认证尚未接入。</p>
    </div>
    <section className="section" aria-label="双创三库">
      <h2 className="section-title">三库资源</h2>
      <p><Link className="section-link" href="/innovation/projects">项目库</Link> · <Link className="section-link" href="/innovation/achievements">校内成果</Link> · <Link className="section-link" href="/innovation/recruitments">正在招募</Link></p>
      <p className="field-hint">成果须审核通过；招募由有资格的项目负责人独立发布，免审。</p>
      <p className="field-hint">人才库尚未开放 · 导师库尚未开放</p>
    </section>
    <PublicAchievementsView />
    <PublicRecruitmentsView compact />
  </>;
}
