"use client";
/**
 * The admin dashboard island (V1.1 spec §4.1, Wave 2a): ONE aggregate
 * request (`GET /admin/overview`) rendered as the workspace's landing
 * — the census cards (users by role/status), the task-lifecycle
 * distribution, the two pending queues, and today's operational pulse.
 *
 * All numbers are backend-owned (the fixed-key contract: by_role/
 * by_status carry every key, zero values included — the UI renders the
 * full machine, never a data-dependent subset); loading/error ride the
 * shared Section states, and a manual refresh re-fires the one call.
 */
import { useCallback, useEffect, useState } from "react";

import { SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { Button } from "@/components/ui/button";

import {
  fetchAdminOverview,
  type AdminOverviewDto,
} from "./adminPowersApi";

const ROLE_LABELS: Record<string, string> = {
  STUDENT: "学生",
  TEACHER: "教师",
  ADMIN: "管理员",
};

const USER_STATUS_LABELS: Record<string, string> = {
  ACTIVE: "正常",
  PENDING_APPROVAL: "待审核",
  SUSPENDED: "已停用",
  BANNED: "已封禁",
};

const TASK_STATUS_LABELS: Record<string, string> = {
  DRAFT: "草稿",
  PUBLISHED: "已发布",
  PAUSED: "已暂停",
  CLOSED: "已关闭",
  ARCHIVED: "已归档",
};

type Phase = "loading" | "ready" | "error";

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="metric">
      <span className="metric-label">{label}</span>
      <span className="metric-value">{value}</span>
    </div>
  );
}

/** A two-column label/value strip (the task-facts idiom). */
function FactLine({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="fact-row">
      <dt className="fact-label">{label}</dt>
      <dd className="fact-value">{value}</dd>
    </div>
  );
}

export function AdminDashboard() {
  const [phase, setPhase] = useState<Phase>("loading");
  const [data, setData] = useState<AdminOverviewDto | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(() => {
    setPhase("loading");
    setError(null);
  }, []);

  useEffect(() => {
    if (phase !== "loading") {
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const overview = await fetchAdminOverview();
        if (!cancelled) {
          setData(overview);
          setPhase("ready");
        }
      } catch (cause) {
        if (!cancelled) {
          setError(cause);
          setPhase("error");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [phase]);

  return (
    <section className="section" aria-label="管理台概览">
      <div className="section-head">
        <h2 className="section-title">今日运营</h2>
        <Button variant="ghost" onClick={load} disabled={phase === "loading"}>
          刷新
        </Button>
      </div>
      {phase === "loading" ? (
        <SectionSkeleton lines={4} />
      ) : phase === "error" ? (
        <SectionError
          error={error}
          onRetry={load}
          retryLabel="重新加载概览"
        />
      ) : data === null ? null : (
        <>
          <div className="metric-row">
            <Metric label="今日新增用户" value={data.today.new_users} />
            <Metric label="今日活跃用户" value={data.today.active_users} />
            <Metric
              label="待审提交"
              value={data.pending_submissions}
            />
            <Metric
              label="待审兑换"
              value={data.pending_redemptions}
            />
          </div>

          <h3 className="section-title">用户</h3>
          <div className="metric-row">
            <Metric label="用户总数" value={data.users.total} />
            {Object.entries(data.users.by_role).map(([role, count]) => (
              <Metric key={role} label={ROLE_LABELS[role] ?? role} value={count} />
            ))}
          </div>
          <dl className="fact-rows">
            {Object.entries(data.users.by_status).map(([status, count]) => (
              <FactLine
                key={status}
                label={USER_STATUS_LABELS[status] ?? status}
                value={count}
              />
            ))}
          </dl>

          <h3 className="section-title">任务状态分布</h3>
          <dl className="fact-rows">
            {Object.entries(data.tasks.by_status).map(([status, count]) => (
              <FactLine
                key={status}
                label={TASK_STATUS_LABELS[status] ?? status}
                value={count}
              />
            ))}
          </dl>
        </>
      )}
    </section>
  );
}
