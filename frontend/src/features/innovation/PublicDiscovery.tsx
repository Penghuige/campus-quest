"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import {
  SectionError,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { useSection, type SectionState } from "@/components/ui/useSection";
import { useSession } from "@/features/auth/session";
import { getAuthEpoch } from "@/lib/accessToken";
import type { components } from "@/lib/api/schema";

import { getPublicProjects, getPublicRecruitments, type PublicRecruitmentDto } from "./recruitmentApi";
import { recruitmentDateTime } from "./recruitmentPresentation";
import { listPublicAchievements, type PublicAchievementDto } from "./reviewApi";
import { getPublicTalents, type TalentSummaryDto } from "./talentApi";
import { TALENT_STATES } from "./talentForm";
import { TalentTags } from "./TalentOverview";

type ProjectDto = components["schemas"]["PublicProjectSummary"];
type Page<T> = { items: T[] };

const LANDING_PREVIEW_LIMIT = 3;
const HOME_PREVIEW_LIMIT = 2;

function previewParams(limit: number): URLSearchParams {
  return new URLSearchParams({ limit: String(limit), offset: "0" });
}

interface PreviewSectionProps<T> {
  title: string;
  label: string;
  href: string;
  headingLevel?: "h2" | "h3";
  state: SectionState<Page<T>>;
  retry: () => void;
  empty: string;
  children: (items: T[]) => ReactNode;
}

/** One independently loaded content index. A failed section never removes
 * the other discovery paths, and the directory link remains available in
 * loading, empty and error states. */
function PreviewSection<T>({
  title,
  label,
  href,
  headingLevel = "h2",
  state,
  retry,
  empty,
  children,
}: PreviewSectionProps<T>) {
  const Heading = headingLevel;
  return (
    <section className="section discovery-section" aria-label={label}>
      <div className="section-head">
        <Heading className="section-title">{title}</Heading>
        <Link className="section-link" href={href}>
          查看全部
        </Link>
      </div>
      {state.status === "loading" ? <SectionSkeleton lines={2} /> : null}
      {state.status === "error" ? <SectionError error={state.error} onRetry={retry} /> : null}
      {state.status === "ready" ? (
        state.data.items.length > 0 ? (
          <ul className="discovery-list">{children(state.data.items)}</ul>
        ) : (
          <p className="discovery-empty">{empty}</p>
        )
      ) : null}
    </section>
  );
}

function RecruitmentPreview({ limit, headingLevel }: { limit: number; headingLevel?: "h2" | "h3" }) {
  const { state, retry } = useSection(() => getPublicRecruitments(previewParams(limit)));
  const ItemHeading = headingLevel === "h3" ? "h4" : "h3";
  return (
    <PreviewSection<PublicRecruitmentDto>
      title="正在招募"
      label="招募机会"
      href="/innovation/recruitments"
      headingLevel={headingLevel}
      state={state}
      retry={retry}
      empty="暂无有效招募，可以先看看校内项目。"
    >
      {(items) =>
        items.map((item) => (
          <li className="discovery-item" key={item.id}>
            <ItemHeading className="discovery-item-title">
              <Link href={`/innovation/recruitments/${item.id}`}>{item.content.role_name}</Link>
            </ItemHeading>
            <p className="discovery-meta">
              {item.content.project_title} · {item.content.direction} · {item.content.stage}
            </p>
            <p className="discovery-excerpt">{item.content.duties}</p>
            <p className="discovery-facts">
              <span>{item.content.headcount} 人</span>
              <span>{item.content.beginner_friendly ? "零基础友好" : "需要相关基础"}</span>
              {item.content.deadline_at ? (
                <span>
                  截止 <time dateTime={item.content.deadline_at}>{recruitmentDateTime(item.content.deadline_at)}</time>
                </span>
              ) : (
                <span>无截止日期</span>
              )}
            </p>
          </li>
        ))
      }
    </PreviewSection>
  );
}

function ProjectPreview({ limit, headingLevel }: { limit: number; headingLevel?: "h2" | "h3" }) {
  const { state, retry } = useSection(() => getPublicProjects(previewParams(limit)));
  const ItemHeading = headingLevel === "h3" ? "h4" : "h3";
  return (
    <PreviewSection<ProjectDto>
      title="校内项目"
      label="项目发现"
      href="/innovation/projects"
      headingLevel={headingLevel}
      state={state}
      retry={retry}
      empty="暂无公开项目，可以先看看正在招募的岗位。"
    >
      {(items) =>
        items.map((item) => (
          <li className="discovery-item" key={item.id}>
            <ItemHeading className="discovery-item-title">
              <Link href={`/innovation/projects/${item.id}`}>{item.title}</Link>
            </ItemHeading>
            <p className="discovery-meta">
              {item.direction} · {item.stage}
            </p>
            <p className="discovery-excerpt">{item.summary}</p>
            <p className="discovery-facts">
              <span>{item.achievement_count} 份已核实成果</span>
              <span>{item.recruitment_count} 个有效岗位</span>
            </p>
          </li>
        ))
      }
    </PreviewSection>
  );
}

function TalentPreview() {
  const { state, retry } = useSection(() => getPublicTalents(previewParams(LANDING_PREVIEW_LIMIT)));
  return (
    <PreviewSection<TalentSummaryDto>
      title="找同学"
      label="人才发现"
      href="/innovation/talents"
      state={state}
      retry={retry}
      empty="暂无公开人才档案，只有同学主动公开的档案会在这里出现。"
    >
      {(items) =>
        items.map((item) => (
          <li className="discovery-item" key={item.id}>
            <div className="discovery-title-row">
              <h3 className="discovery-item-title">
                <Link href={`/innovation/talents/${item.id}`}>{item.name}</Link>
              </h3>
              <span className="ie-talent-state">{TALENT_STATES[item.cooperation_state]}</span>
            </div>
            <p className="discovery-meta">
              {item.faculty} · {item.grade}
            </p>
            {item.tags.length > 0 ? <TalentTags tags={item.tags.slice(0, 4)} /> : null}
            <p className="discovery-excerpt">{item.advantages}</p>
          </li>
        ))
      }
    </PreviewSection>
  );
}

function AchievementPreview() {
  const { state, retry } = useSection(() => listPublicAchievements(0, LANDING_PREVIEW_LIMIT));
  return (
    <PreviewSection<PublicAchievementDto>
      title="校内成果"
      label="成果发现"
      href="/innovation/achievements"
      state={state}
      retry={retry}
      empty="暂无已核实成果，成果审核通过后会在这里展示。"
    >
      {(items) =>
        items.map((item) => (
          <li className="discovery-item" key={item.id}>
            <h3 className="discovery-item-title">
              <Link href={`/innovation/achievements/${item.id}`}>{item.achievement.title}</Link>
            </h3>
            <p className="discovery-meta">
              {item.project.title} · {item.project.direction}
            </p>
            <p className="discovery-excerpt">{item.achievement.description}</p>
            <p className="discovery-facts">已核实成果 · 查看项目与公开作品</p>
          </li>
        ))
      }
    </PreviewSection>
  );
}

/** Public campus landing: real content comes before taxonomy and rules. */
export function InnovationDiscovery() {
  const { state } = useSession();
  if (state.status !== "authenticated" || state.me.status !== "ACTIVE") return null;
  return (
    <div className="discovery-landing" key={`${state.me.id}:${getAuthEpoch()}`}>
      <RecruitmentPreview limit={LANDING_PREVIEW_LIMIT} />
      <div className="discovery-pair">
        <ProjectPreview limit={LANDING_PREVIEW_LIMIT} />
        <TalentPreview />
      </div>
      <AchievementPreview />
    </div>
  );
}

/** A small cross-module path on the student dashboard, not a second feed. */
export function HomeDiscovery() {
  const { state } = useSession();
  if (state.status !== "authenticated" || state.me.status !== "ACTIVE") return null;
  return (
    <section className="section home-discovery" aria-label="校园发现" key={`${state.me.id}:${getAuthEpoch()}`}>
      <div className="section-head">
        <div className="discovery-intro">
          <h2 className="section-title">校园里正在发生</h2>
          <p className="field-hint">从具体项目和岗位开始，找到任务之外的实践机会。</p>
        </div>
        <Link className="section-link" href="/innovation">
          进入创新创业
        </Link>
      </div>
      <div className="discovery-pair">
        <RecruitmentPreview limit={HOME_PREVIEW_LIMIT} headingLevel="h3" />
        <ProjectPreview limit={HOME_PREVIEW_LIMIT} headingLevel="h3" />
      </div>
    </section>
  );
}
