"use client";
import Link from "next/link";
import { EmptyState, SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { useSession } from "@/features/auth/session";
import { getAuthEpoch } from "@/lib/accessToken";
import { getPublicProject, getPublicProjects } from "./recruitmentApi";
import { RecruitmentRows, PublicResourceError } from "./PublicRecruitmentsView";

export function PublicProjectsView({ id, query = "" }: { id?: string; query?: string }) {
  const { state } = useSession();
  if (state.status !== "authenticated" || state.me.status !== "ACTIVE") return null;
  const key = `${state.me.id}:${getAuthEpoch()}:${id ?? ""}:${query}`;
  return id ? <ProjectDetail key={key} id={id} query={query} /> : <ProjectPage key={key} query={query} />;
}
function ProjectPage({ query }: { query: string }) {
  const params = new URLSearchParams(query);
  const { state, retry } = useSection(() => getPublicProjects(params));
  return <section className="section" aria-label="校内项目列表"><CampusFilters path="/innovation/projects" params={params} />{state.status === "loading" ? <SectionSkeleton /> : state.status === "error" ? <SectionError error={state.error} onRetry={retry} /> : <>{state.data.items.length ? <ul className="ie-draft-list">{state.data.items.map((item) => <li className="ie-draft-row" key={item.id}><div className="ie-draft-copy"><h2 className="section-title"><Link href={`/innovation/projects/${item.id}`}>{item.title}</Link></h2><p>{item.direction} · {item.stage}</p><p className="ie-draft-excerpt">{item.summary}</p><p className="field-hint">介绍来源：{item.source_kind === "ACHIEVEMENT" ? "已核实成果版本" : "负责人发布的招募版本（免审）"} · {item.achievement_count} 份成果 · {item.recruitment_count} 个有效岗位</p></div></li>)}</ul> : <EmptyState title="暂无符合条件的项目" hint="已核实成果或有效招募项目将在这里展示。" />}<CampusPagination path="/innovation/projects" params={params} offset={state.data.offset} total={state.data.total} /></>}</section>;
}
function ProjectDetail({ id, query }: { id: string; query: string }) {
  const params = new URLSearchParams(query);
  const { state, retry } = useSection(() => getPublicProject(id, params));
  if (state.status === "loading") return <><h1 className="page-title">校内项目</h1><SectionSkeleton /></>;
  if (state.status === "error") return <><h1 className="page-title">校内项目</h1><Link href="/innovation/projects">返回项目库</Link><PublicResourceError error={state.error} retry={retry} /></>;
  const { project, achievements, recruitments } = state.data;
  return <article className="section ie-recruitment-public" aria-label="校内项目详情"><Link href="/innovation/projects">返回项目库</Link><h1 className="page-title">{project.title}</h1><p>{project.direction} · {project.stage}</p><p className="ie-review-prose">{project.summary}</p><p className="field-hint">介绍来源：{project.source_kind === "ACHIEVEMENT" ? "已核实成果版本" : "负责人发布的招募版本（免审）"}</p><section className="section" aria-label="项目已核实成果"><h2 className="section-title">已核实成果</h2>{achievements.items.length ? <ul className="ie-draft-list">{achievements.items.map((item) => <li key={item.id} className="ie-draft-row"><div className="ie-draft-copy"><h3 className="section-title"><Link href={`/innovation/achievements/${item.id}`}>{item.achievement.title}</Link></h3><p className="ie-draft-excerpt">{item.achievement.description}</p></div></li>)}</ul> : <EmptyState title="暂无已核实成果" hint="招募无需依赖已核实成果。" />}<CampusPagination path={`/innovation/projects/${id}`} params={params} offset={achievements.offset} total={achievements.total} keyName="achievement_offset" label="项目成果分页" /></section><section className="section" aria-label="项目正在招募"><h2 className="section-title">正在招募</h2>{recruitments.items.length ? <RecruitmentRows items={recruitments.items} /> : <EmptyState title="暂无有效招募" hint="已关闭或截止的岗位不会展示。" />}<CampusPagination path={`/innovation/projects/${id}`} params={params} offset={recruitments.offset} total={recruitments.total} keyName="recruitment_offset" label="项目招募分页" /></section></article>;
}
export function CampusFilters({ path, params }: { path: string; params: URLSearchParams }) {
  return <form method="get" action={path} className="form ie-draft-form" aria-label="校内浏览筛选">{([ ["q", "搜索"], ["direction", "方向"], ["stage", "阶段"] ] as const).map(([key, label]) => <div className="field" key={key}><label className="field-label" htmlFor={`filter-${key}`}>{label}</label><input className="input" id={`filter-${key}`} name={key} defaultValue={params.get(key) ?? ""} maxLength={key === "stage" ? 80 : 120} /></div>)}<div className="field"><label className="field-label" htmlFor="filter-beginner">零基础友好</label><select className="input" id="filter-beginner" name="beginner_friendly" defaultValue={params.get("beginner_friendly") ?? ""}><option value="">不限</option><option value="true">是</option><option value="false">否</option></select></div><label className="field-label"><input type="checkbox" name="recruiting_only" value="true" defaultChecked={params.get("recruiting_only") === "true"} /> 仅看正在招募</label><div className="ie-draft-actions"><button type="submit" className="btn btn-primary">筛选</button><Link href={path}>清空筛选</Link></div></form>;
}
export function CampusPagination({ path, params, offset, total, keyName = "offset", label = "校内浏览分页" }: { path: string; params: URLSearchParams; offset: number; total: number; keyName?: string; label?: string }) {
  function href(next: number) { const query = new URLSearchParams(params); query.set(keyName, String(next)); return `${path}?${query}`; }
  return <nav className="ie-draft-actions" aria-label={label}>{offset > 0 ? <Link href={href(Math.max(0, offset - 20))}>上一页</Link> : null}<span className="field-hint">第 {Math.floor(offset / 20) + 1} 页 · 共 {total} 项</span>{offset + 20 < total ? <Link href={href(offset + 20)}>下一页</Link> : null}</nav>;
}
