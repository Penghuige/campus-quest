"use client";
import Link from "next/link";
import { EmptyState, SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { useSession } from "@/features/auth/session";
import { getAuthEpoch } from "@/lib/accessToken";
import { BUSINESS_TIME_CONFIG } from "@/lib/time";
import { isApiError } from "@/lib/errors";
import { recruitmentDateTime } from "./recruitmentPresentation";
import { getPublicRecruitment, getPublicRecruitments, type PublicRecruitmentDto } from "./recruitmentApi";
import { RecruitmentContacts } from "./RecruitmentContacts";
import { CampusFilters, CampusPagination } from "./PublicProjectsView";

export function PublicRecruitmentsView({ id, query = "", compact = false }: { id?: string; query?: string; compact?: boolean }) {
  const { state } = useSession();
  if (state.status !== "authenticated" || state.me.status !== "ACTIVE") return null;
  const key = `${state.me.id}:${getAuthEpoch()}:${id ?? query}`;
  return id ? <RecruitmentDetail key={key} id={id} /> : <RecruitmentPage key={key} query={query} compact={compact} />;
}
function RecruitmentPage({ query, compact }: { query: string; compact: boolean }) {
  const params = new URLSearchParams(query);
  const { state, retry } = useSection(() => getPublicRecruitments(params));
  return <section className="section" aria-label="校内招募岗位">{compact ? <div className="section-head"><h2 className="section-title">正在招募</h2><Link href="/innovation/recruitments">浏览全部招募</Link></div> : <CampusFilters path="/innovation/recruitments" params={params} showRecruitingOnly={false} />}{state.status === "loading" ? <SectionSkeleton /> : state.status === "error" ? <SectionError error={state.error} onRetry={retry} /> : <>{state.data.items.length ? <RecruitmentRows items={state.data.items} /> : <EmptyState title="暂无符合条件的招募" hint="当前有效且有联系授权的岗位将在这里展示，可以调整筛选或稍后再来。" />}{!compact ? <CampusPagination path="/innovation/recruitments" params={params} offset={state.data.offset} total={state.data.total} /> : null}</>}</section>;
}
export function RecruitmentRows({ items }: { items: PublicRecruitmentDto[] }) {
  return <ul className="ie-draft-list">{items.map((item) => <li className="ie-draft-row" key={item.id}><div className="ie-draft-copy"><h3 className="section-title"><Link href={`/innovation/recruitments/${item.id}`}>{item.content.role_name}</Link></h3><p>{item.content.project_title} · {item.content.direction} · {item.content.stage}</p><p>{item.content.headcount} 人 · {item.content.beginner_friendly ? "零基础友好" : "不接受零基础"}</p><RecruitmentDeadline value={item.content.deadline_at} /><p className="ie-draft-excerpt">{item.content.duties}</p><p className="field-hint">招募内容由负责人独立发布，免审，未经成果核实。</p></div></li>)}</ul>;
}
function RecruitmentDetail({ id }: { id: string }) {
  const { state, retry } = useSection(() => getPublicRecruitment(id));
  if (state.status === "loading") return <><h1 className="page-title">招募岗位</h1><SectionSkeleton /></>;
  if (state.status === "error") return <><h1 className="page-title">招募岗位</h1><Link href="/innovation/recruitments">返回招募列表</Link><PublicResourceError error={state.error} retry={retry} /></>;
  const item = state.data; const content = item.content;
  return <article className="section ie-recruitment-public" aria-label="校内招募详情"><Link href="/innovation/recruitments">返回招募列表</Link><h1 className="page-title">{content.role_name}</h1><p className="field-hint">正在招募 · 负责人独立发布，免审，未经成果核实</p><p>{content.headcount} 人 · {content.beginner_friendly ? "零基础友好" : "不接受零基础"}</p><RecruitmentDeadline value={content.deadline_at} /><section className="section" aria-label="招募项目介绍"><h2 className="section-title"><Link href={`/innovation/projects/${item.project_id}`}>{content.project_title}</Link></h2><p>{content.direction} · {content.idea_stage && content.stage !== "创意起步" ? `创意起步（${content.stage}）` : content.stage}</p><p className="ie-review-prose">{content.introduction}</p>{content.idea_stage ? <><h3 className="section-title">阶段目标</h3><p className="ie-review-prose">{content.goal}</p><h3 className="section-title">初步方案</h3><p className="ie-review-prose">{content.initial_plan}</p></> : null}</section><dl className="ie-review-prose">{([ ["主要职责", content.duties], ["能力要求", content.requirements], ["时间投入", content.time_commitment] ] as const).map(([label, value]) => <div key={label}><dt className="field-label">{label}</dt><dd>{value}</dd></div>)}</dl><RecruitmentContacts key={item.revision_number} item={item} /></article>;
}
function RecruitmentDeadline({ value }: { value: string | null }) {
  return <p className="field-hint">{value ? <>截止：<time dateTime={value}>{recruitmentDateTime(value)}</time>（{BUSINESS_TIME_CONFIG.timeZone}）</> : "无截止日期 · 负责人每 30 天收到确认提醒"}</p>;
}

export function PublicResourceError({ error, retry }: { error: unknown; retry: () => void }) {
  return isApiError(error) && error.status === 404 ? <p className="alert alert-error" role="alert">内容不存在或已停止校内展示，请返回列表查看当前有效内容。</p> : <SectionError error={error} onRetry={retry} />;
}
