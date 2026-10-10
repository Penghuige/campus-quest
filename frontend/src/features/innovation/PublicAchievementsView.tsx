"use client";
import Link from "next/link";
import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { EmptyState, SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { useSession } from "@/features/auth/session";
import { getAuthEpoch } from "@/lib/accessToken";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";
import { validWorkLink } from "./achievementForm";
import { getPublicAchievement, listPublicAchievements, type PublicAchievementDto } from "./reviewApi";
import { publicReviewText } from "./reviewPresentation";
import { StudentShell } from "@/app/(student)/_components/StudentShell";

/** Campus reads accept every active school role, unlike the student workspace. */
export function CampusAchievementShell({ children }: { children: ReactNode }) {
  const { state, refresh } = useSession();
  if (state.status === "authenticated" && state.me.role === "STUDENT" && state.me.status === "ACTIVE") return <StudentShell>{children}</StudentShell>;
  const workspace = state.status === "authenticated" && state.me.role === "ADMIN" ? "/admin/users" : state.status === "authenticated" && state.me.role === "TEACHER" ? "/teacher/reviews" : "/";
  return <div className="app-shell"><header className="app-topbar"><div className="app-topbar-inner"><span className="app-brand">CampusQuest</span><nav className="ie-draft-actions" aria-label="校内浏览导航"><Link href="/innovation">创新创业</Link><Link href={workspace}>返回工作台</Link>{state.status === "authenticated" ? <Link href="/logout" prefetch={false}>退出登录</Link> : null}</nav></div></header><main className="app-main task-detail">
    {state.status === "loading" ? <SectionSkeleton /> : state.status === "error" ? <SectionError error={state.error} onRetry={refresh} /> : state.status === "anonymous" ? <p className="alert">请先<Link href="/login">登录校内账号</Link>后浏览成果。</p> : state.me.status !== "ACTIVE" ? <p className="alert alert-warning" role="alert">当前账号状态不允许浏览校内成果。</p> : <div key={`${state.me.id}:${getAuthEpoch()}`}>{children}</div>}
  </main></div>;
}
export function PublicAchievementsView({ id }: { id?: string }) {
  const { state } = useSession();
  if (state.status !== "authenticated" || state.me.status !== "ACTIVE") return null;
  return id ? <PublicDetail key={`${state.me.id}:${getAuthEpoch()}:${id}`} id={id} /> : <PublicList key={`${state.me.id}:${getAuthEpoch()}`} />;
}
function PublicList() {
  const [offset, setOffset] = useState(0);
  return <PublicPage key={offset} offset={offset} onPage={setOffset} />;
}
function PublicPage({ offset, onPage }: { offset: number; onPage: (value: number) => void }) {
  const { state, retry } = useSection(() => listPublicAchievements(offset));
  if (state.status === "loading") return <SectionSkeleton />;
  if (state.status === "error") return <SectionError error={state.error} onRetry={retry} />;
  return <section className="section" aria-label="校内已核实成果">
    {state.data.items.length ? <ul className="ie-draft-list">{state.data.items.map((item) => <li className="ie-draft-row" key={item.id}><div className="ie-draft-copy"><h2 className="section-title"><Link href={`/innovation/achievements/${item.id}`}>{item.achievement.title}</Link></h2><p>{item.project.title} · {item.project.stage}</p><p className="ie-draft-excerpt">{item.achievement.description}</p><PublicationDates item={item} /></div></li>)}</ul> : <EmptyState title="暂无可浏览的成果" hint="首次核实通过的成果将在这里展示。" />}
    <nav className="ie-draft-actions" aria-label="校内成果分页"><Button variant="secondary" disabled={!offset} onClick={() => onPage(Math.max(0, offset - 20))}>上一页</Button><span className="field-hint">第 {Math.floor(offset / 20) + 1} 页 · 共 {state.data.total} 份</span><Button variant="secondary" disabled={offset + 20 >= state.data.total} onClick={() => onPage(offset + 20)}>下一页</Button></nav>
  </section>;
}
function PublicDetail({ id }: { id: string }) {
  const { state, retry } = useSection(() => getPublicAchievement(id));
  if (state.status === "loading") return <SectionSkeleton />;
  if (state.status === "error") return <SectionError error={state.error} onRetry={retry} />;
  const item = state.data;
  return <article className="section" aria-label="校内成果详情"><h1 className="page-title">{item.achievement.title}</h1><PublicationDates item={item} /><p className="ie-review-prose">{item.achievement.description}</p>
    <section className="panel ie-draft-remote" aria-label="公开项目概况"><h2 className="section-title">{item.project.title}</h2><dl>{([ ["项目简介", item.project.summary], ["研究方向", item.project.direction], ["项目阶段", item.project.stage], ["团队情况", item.project.team_status], ["立项或获奖说明", item.achievement.award_text] ] as const).map(([label, value]) => <div key={label}><dt className="field-label">{label}</dt><dd>{value || "未填写"}</dd></div>)}</dl></section>
    {validWorkLink(item.achievement.work_url) ? <a href={item.achievement.work_url} target="_blank" rel="noopener noreferrer">打开公开作品链接</a> : null}
  </article>;
}
function PublicationDates({ item }: { item: PublicAchievementDto }) {
  return <div><p className="field-hint">首次核实：<time dateTime={item.first_approved_at}>{formatDeadlineDateTime(parseServerInstant(item.first_approved_at))}</time> · {item.latest_reviewed_at ? <>本版核实：<time dateTime={item.latest_reviewed_at}>{formatDeadlineDateTime(parseServerInstant(item.latest_reviewed_at))}</time></> : <>最近发布：<time dateTime={item.updated_at}>{formatDeadlineDateTime(parseServerInstant(item.updated_at))}</time></>}</p><p className={item.updated_after_first_review && !item.latest_reviewed_at ? "alert" : "field-hint"}>{publicReviewText(item)}</p></div>;
}
