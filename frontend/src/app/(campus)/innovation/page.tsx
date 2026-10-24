import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = { title: "创新创业 · 华师令" };

export default function InnovationPage() {
  return <>
    <div className="page-head">
      <h1 className="page-title">创新创业</h1>
      <p className="page-subtitle">人才库已开放，找同学、看技能、按授权联系</p>
      <p className="field-hint">演示阶段：使用预置账号；演示资料为模拟内容。学校统一身份认证尚未接入。</p>
    </div>
    <section className="section" aria-label="双创板块入口">
      <h2 className="section-title">板块入口</h2>
      <p><Link className="section-link" href="/innovation/talents">人才库 · 找同学</Link></p>
      <p className="field-hint">人才档案由同学本人主动确认公开，资料为自述，文件安全检查不代表内容真实性核实。</p>
      <p className="field-hint">项目库、校内成果与正在招募即将开放；导师库尚未开放。</p>
    </section>
  </>;
}
