/**
 * Pure growth-page views (spec §18/§19; patterns §3). Every figure is
 * the caller's own and arrives server-computed (backend
 * rankings/growth_service): this module only maps numbers onto display
 * text — month rank null means 未上榜 (the server's "no score on the
 * board"), the on-time ratio formats from the server's float, and the
 * streak counts CONSECUTIVE ON-TIME COMPLETIONS (tasks, not calendar
 * days — growth_service `current_on_time_streak`).
 *
 * Known gap (documented for the next backend round): the snapshot has NO
 * display-honor selector API — `display_honor` exists only as a
 * server-chosen field on ranking rows, and /growth/me carries no
 * "selected honor" field or mutation endpoint. The honors list below is
 * therefore read-only; a selector lands with that endpoint.
 */
import { BUSINESS_TIME_CONFIG, type BusinessTimeConfig } from "@/lib/time";

import type { GrowthDto, OwnedHonorDto } from "@/features/rankings/api";

// --- summary metrics ---------------------------------------------------------------

export interface GrowthSummaryView {
  monthPoints: number;
  /** "第 N 名"; "未上榜" while the server says month_rank=null. */
  monthRankText: string;
  totalEarnedPoints: number;
  completedCount: number;
  /** "18 / 20 次（90%）"-style on-time line. */
  onTimeText: string;
  /** Percent 0-100 with one decimal when needed; null when no completions. */
  onTimeRatioPercent: number | null;
  /** Consecutive on-time completions, tasks. */
  streakText: string;
  /** "第 N 名" for the best historical monthly rank; null when none. */
  bestRankText: string | null;
}

/** Format the server's 0..1 ratio as a percent string ("90%" / "87.5%"). */
export function formatRatioPercent(ratio: number): string {
  const percent = Math.round(ratio * 1000) / 10;
  return Number.isInteger(percent) ? `${percent}%` : `${percent}%`;
}

export function growthSummaryView(growth: GrowthDto): GrowthSummaryView {
  return {
    monthPoints: growth.month_points,
    monthRankText:
      growth.month_rank === null ? "未上榜" : `第 ${growth.month_rank} 名`,
    totalEarnedPoints: growth.total_earned_points,
    completedCount: growth.completed_count,
    onTimeText: `${growth.on_time_count} / ${growth.completed_count} 次${
      growth.completed_count > 0
        ? `（${formatRatioPercent(growth.on_time_ratio)}）`
        : ""
    }`,
    onTimeRatioPercent:
      growth.completed_count > 0
        ? Math.round(growth.on_time_ratio * 1000) / 10
        : null,
    streakText: `连续按时 ${growth.current_streak} 次`,
    bestRankText:
      growth.best_month_rank === null
        ? null
        : `第 ${growth.best_month_rank} 名`,
  };
}

// --- honors ------------------------------------------------------------------------

/** Design §9: honor_type is a category, rendered as wording not enum. */
export const HONOR_TYPE_LABELS: Record<string, string> = {
  TOTAL_COMPLETED: "累计完成",
  ON_TIME_STREAK: "按时连续",
  DAILY_RANK: "日榜",
  MONTHLY_RANK: "月榜",
  TOTAL_EARNED_POINTS: "累计积分",
  COMMEMORATIVE: "纪念",
};

export function honorTypeLabel(honorType: string): string {
  return HONOR_TYPE_LABELS[honorType] ?? "荣誉";
}

export interface HonorRowView {
  honorId: string;
  name: string;
  typeLabel: string;
  /** The server period key (YYYY-MM-DD / YYYY-MM) or null; shown as-is. */
  period: string | null;
  /** zh-CN date in the business timezone (year included — honors age). */
  grantedLabel: string;
}

export function honorRowView(
  honor: OwnedHonorDto,
  config: BusinessTimeConfig = BUSINESS_TIME_CONFIG,
): HonorRowView {
  return {
    honorId: honor.honor_id,
    name: honor.name,
    typeLabel: honorTypeLabel(honor.honor_type),
    period: honor.period,
    grantedLabel: formatHonorDate(honor.granted_at, config),
  };
}

function formatHonorDate(iso: string, config: BusinessTimeConfig): string {
  return new Intl.DateTimeFormat(config.locale, {
    timeZone: config.timeZone,
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(new Date(Date.parse(iso)));
}

export interface GrowthView {
  summary: GrowthSummaryView;
  honors: HonorRowView[];
}

/** The full §19 page view: summary metrics plus the owned-honors rows. */
export function growthView(growth: GrowthDto): GrowthView {
  return {
    summary: growthSummaryView(growth),
    honors: growth.honors.map((honor) => honorRowView(honor)),
  };
}
