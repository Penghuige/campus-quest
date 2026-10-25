import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = { title: "项目库 · 华师令" };

/**
 * Batch 2 opens the project-library route so the innovation shell has
 * no dead entry. The public project list is assembled from verified
 * achievements and active recruitments (batches 3/4), so this batch
 * shows an honest hand-off instead of a list backed by missing APIs.
 * The owner-side project drafts live under the student profile and are
 * fully usable now.
 */
export default function ProjectsPage() {
  return (
    <>
      <div className="page-head">
        <Link className="section-link" href="/innovation">返回创新创业</Link>
        <h1 className="page-title">项目库</h1>
        <p className="page-subtitle">浏览已核实成果及负责人独立发布的招募项目。</p>
      </div>
      <section className="section" aria-label="项目库公开展示尚未开放">
        <h2 className="section-title">公开展示即将开放</h2>
        <p className="field-hint">
          项目库的公开展示依赖已核实成果与招募信息，将随后续批次开放。学生现在可以在个人档案的「我的项目」中管理自己的项目草稿。
        </p>
        <p><Link className="section-link" href="/profile/project-drafts">前往我的项目</Link></p>
      </section>
    </>
  );
}
