import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = { title: "创新创业 · 华师令" };

export default function InnovationPage() {
  return <>
    <div className="page-head">
      <h1 className="page-title">创新创业</h1>
      <p className="page-subtitle">浏览校内成果、项目与人才，了解项目与实践方向</p>
      <p className="field-hint">演示阶段：使用预置账号；演示资料为模拟内容。学校统一身份认证尚未接入。</p>
    </div>
    <section className="section" aria-label="双创板块入口">
      <h2 className="section-title">板块入口</h2>
      <p><Link className="section-link" href="/innovation/talents">人才库 · 找同学</Link> · <Link className="section-link" href="/innovation/projects">项目库</Link> · <Link className="section-link" href="/innovation/achievements">校内成果</Link></p>
      <p className="field-hint">校内成果须审核通过后才公开；人才档案由同学本人主动确认公开，资料为自述。</p>
      <p className="field-hint">学生可在「我的档案」的「我的项目」中管理项目草稿与成果草稿。正在招募即将开放；导师库尚未开放。</p>
    </section>
  </>;
}
