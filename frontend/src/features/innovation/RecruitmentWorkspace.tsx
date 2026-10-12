"use client";
import { useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { EmptyState, SectionError, SectionHeading, SectionSkeleton } from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { useSession } from "@/features/auth/session";
import { getAuthEpoch } from "@/lib/accessToken";
import { getProjectDraft } from "./api";
import { getOwnerQualification } from "./qualificationApi";
import { getRecruitment, listRecruitments } from "./recruitmentApi";
import { recruitmentStatusText } from "./recruitmentPresentation";
import { RecruitmentEditor } from "./RecruitmentEditor";
import { getPendingRecruitment } from "./recruitmentPending";
import type { RecruitmentProjectDefaults } from "./recruitmentForm";
import { getRecruitmentRecovery } from "./recruitmentRecovery";

export function RecruitmentWorkspace({ projectId }: { projectId: string }) {
  const { state } = useSession();
  if (state.status !== "authenticated") return null;
  if (state.me.role !== "STUDENT" || state.me.status !== "ACTIVE") return <p className="alert" role="alert">仅正常状态的学生负责人可以管理本人的招募。</p>;
  return <WorkspaceGuard key={`${state.me.id}:${getAuthEpoch()}:${projectId}`} projectId={projectId} />;
}
function WorkspaceGuard({ projectId }: { projectId: string }) {
  const { state, retry } = useSection(async () => { const [project, qualification] = await Promise.all([getProjectDraft(projectId), getOwnerQualification()]); return { project, qualification }; });
  if (state.status === "loading") return <SectionSkeleton />;
  if (state.status === "error") return <SectionError error={state.error} onRetry={retry} />;
  return <><p className="page-subtitle">{state.data.project.title}</p>{state.data.qualification.status !== "APPROVED" ? <p className="alert" role="alert">可准备草稿；发布须先<Link href="/profile/owner-profile">申请并开通负责人资格</Link>。招募独立免审，无需已有审核成果。</p> : null}<Workspace projectId={projectId} projectDefaults={state.data.project} /></>;
}
function Workspace({ projectId, projectDefaults }: { projectId: string; projectDefaults: RecruitmentProjectDefaults }) {
  const [selected, setSelected] = useState<string | null | undefined>(() => {
    const resumed = getPendingRecruitment(projectId);
    const recovered = getRecruitmentRecovery(projectId);
    return resumed ? resumed.command.id : recovered ? recovered.record?.id ?? null : undefined;
  });
  const [offset, setOffset] = useState(0);
  if (selected !== undefined) return selected === null || getPendingRecruitment(projectId) || getRecruitmentRecovery(projectId) ? <RecruitmentEditor projectId={projectId} projectDefaults={projectDefaults} onBack={() => setSelected(undefined)} /> : <EditorLoader projectId={projectId} id={selected} onBack={() => setSelected(undefined)} />;
  return <section className="section" aria-label="招募岗位列表"><SectionHeading title="招募岗位" action={<Button onClick={() => setSelected(null)}>新建招募岗位</Button>} /><RecruitmentList key={offset} projectId={projectId} offset={offset} onPage={setOffset} onEdit={setSelected} /></section>;
}
function RecruitmentList({ projectId, offset, onPage, onEdit }: { projectId: string; offset: number; onPage: (offset: number) => void; onEdit: (id: string) => void }) {
  const { state, retry } = useSection(() => listRecruitments(projectId, offset));
  if (state.status === "loading") return <SectionSkeleton />;
  if (state.status === "error") return <SectionError error={state.error} onRetry={retry} />;
  return <>{state.data.items.length ? <ul className="ie-draft-list">{state.data.items.map((item) => <li className="ie-draft-row" key={item.id}><div className="ie-draft-copy"><h3 className="section-title">{item.fields.role_name || "未命名岗位"}</h3><p>{recruitmentStatusText(item.status, item.moderation_state)}{item.has_unpublished_changes ? " · 有未发布改动" : ""}</p></div><Button variant="secondary" aria-label={`编辑招募：${item.fields.role_name || "未命名岗位"}`} onClick={() => onEdit(item.id)}>编辑</Button></li>)}</ul> : <EmptyState title="还没有招募岗位" hint="先保存草稿，再预览发布内容与联系授权。" />}<nav className="ie-draft-actions" aria-label="本人招募分页"><Button variant="secondary" disabled={!offset} onClick={() => onPage(Math.max(0, offset - 20))}>上一页</Button><span className="field-hint">第 {Math.floor(offset / 20) + 1} 页 · 共 {state.data.total} 个</span><Button variant="secondary" disabled={offset + 20 >= state.data.total} onClick={() => onPage(offset + 20)}>下一页</Button></nav></>;
}
function EditorLoader({ projectId, id, onBack }: { projectId: string; id: string; onBack: () => void }) {
  const { state, retry } = useSection(() => getRecruitment(projectId, id));
  if (state.status === "loading") return <SectionSkeleton />;
  if (state.status === "error") return <><SectionError error={state.error} onRetry={retry} /><Button variant="secondary" onClick={onBack}>返回招募列表</Button></>;
  return <RecruitmentEditor projectId={projectId} initial={state.data} onBack={onBack} />;
}
