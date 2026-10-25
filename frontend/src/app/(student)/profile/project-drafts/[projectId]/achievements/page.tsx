import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = { title: "我的成果草稿" };

/**
 * Batch 2 lands only the route shell for the achievement drafts page:
 * the draft workflow and its APIs arrive with batch 3. Mounting the
 * future view here would ship a page whose data layer does not exist,
 * so the placeholder states the boundary honestly.
 */
export default function AchievementDraftsPage() {
  return (
    <>
      <div className="page-head">
        <Link className="section-link" href="/profile/project-drafts">返回项目草稿</Link>
        <h1 className="page-title">我的成果草稿</h1>
        <p className="page-subtitle">在项目下整理作品与成果，准备好后再进入后续核实流程。</p>
      </div>
      <section className="section" aria-label="成果草稿尚未开放">
        <h2 className="section-title">成果草稿即将开放</h2>
        <p className="field-hint">
          本批先开放项目草稿与负责人资料；成果草稿与核实流程将随后续批次开放。
        </p>
        <p><Link className="section-link" href="/profile/project-drafts">返回我的项目</Link></p>
      </section>
    </>
  );
}
