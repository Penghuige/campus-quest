import type { Metadata } from "next";
import Link from "next/link";

import { InnovationDiscovery } from "@/features/innovation/PublicDiscovery";

export const metadata: Metadata = { title: "创新创业 · CampusQuest" };

export default function InnovationPage() {
  return (
    <>
      <div className="page-head">
        <h1 className="page-title">创新创业</h1>
        <p className="page-subtitle">
          看看同学们正在做什么、哪些项目正在招募，从一个具体机会开始了解双创。
        </p>
      </div>

      <InnovationDiscovery />

      <section className="discovery-directory" aria-label="双创板块入口">
        <h2 className="section-title">按内容类型浏览</h2>
        <p className="discovery-directory-links">
          <Link className="section-link" href="/innovation/talents">人才库 · 找同学</Link>
          <Link className="section-link" href="/innovation/projects">项目库</Link>
          <Link className="section-link" href="/innovation/achievements">校内成果</Link>
          <Link className="section-link" href="/innovation/recruitments">正在招募</Link>
        </p>
        <details className="discovery-rules">
          <summary>内容公开与审核说明</summary>
          <div className="discovery-rules-body">
            <p>项目库汇总已核实成果与负责人独立发布的招募项目；招募免审，内容未经成果核实。</p>
            <p>校内成果须审核通过后才公开；人才档案由同学本人主动确认公开，资料为自述。</p>
            <p>学生可在「我的」的「我的项目」中管理项目草稿、成果草稿与招募岗位。导师库尚未开放。</p>
            <p>演示阶段使用预置账号和模拟资料，学校统一身份认证尚未接入。</p>
          </div>
        </details>
      </section>
    </>
  );
}
