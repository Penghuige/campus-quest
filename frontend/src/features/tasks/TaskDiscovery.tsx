"use client";
/**
 * Task discovery island (patterns §8 dashboard discovery, §9 pagination):
 * one offset page of /tasks at a time with a "load more" control —
 * server-backed pagination (the query is the contract; nothing is
 * filtered or counted client-side), with the §10 triad for the first
 * load and an inline retry that keeps already-loaded cards visible.
 *
 * State-shape note (lint-driven): fetchers are setState-free; the mount
 * effect only starts a fetch and applies results in async callbacks, so
 * effects never cascade renders synchronously.
 *
 * Batch ① (flicker, 2026-10-10): the first page rides the shared data
 * cache via `useSection` — a REMOUNTING square (back-navigation from a
 * task detail) renders its cached snapshot synchronously instead of a
 * skeleton-flash-refetch cycle (the exact QA #2 contract the dashboard
 * sections already ride; the square was the missed adoption surface).
 * Load-more pages stay uncached by design: offset pagination over a
 * mutating availability surface means only the server's per-page
 * verdict is authoritative, and appended pages always reflect the
 * moment of their click.
 */
import { useCallback, useState, type CSSProperties } from "react";

import {
  EmptyState,
  SectionCardsSkeleton,
  SectionError,
} from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";

import { listTasks, type TaskCardDto, type TaskListPageDto } from "./api";
import { entranceStaggerMs } from "./display";
import { TaskCard } from "./TaskCard";
import { useNow } from "./useNow";
import { Button } from "@/components/ui/button";

const PAGE_SIZE = 12;

export function TaskDiscovery() {
  // Minute-granularity text; the card countdown is display-only (§42).
  const now = useNow(60_000);
  const { state, retry } = useSection(
    () => listTasks({ limit: PAGE_SIZE, offset: 0 }),
    `tasks:list:${PAGE_SIZE}:0`,
  );
  // Load-more appends are mount-local state (see the module note: they
  // are deliberately NOT cached across navigations).
  const [appended, setAppended] = useState<TaskCardDto[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);

  const loadMore = useCallback(async () => {
    if (loadingMore) {
      return;
    }
    setLoadingMore(true);
    setMoreError(null);
    try {
      const base = state.status === "ready" ? state.data.items.length : 0;
      const page: TaskListPageDto = await listTasks({
        limit: PAGE_SIZE,
        offset: base + appended.length,
      });
      setAppended((previous) => [...previous, ...page.items]);
    } catch (cause) {
      setMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [appended.length, loadingMore, state]);

  if (state.status === "loading") {
    return <SectionCardsSkeleton cards={6} />;
  }
  if (state.status === "error") {
    return <SectionError error={state.error} onRetry={retry} retryLabel="重新加载" />;
  }

  const items = [...state.data.items, ...appended];
  const total = state.data.total;
  if (items.length === 0) {
    return (
      <EmptyState
        title="暂无可领取的任务"
        hint="老师还没有发布可领取的任务，稍后再来看看"
      />
    );
  }

  const canLoadMore = total !== null && items.length < total;
  // Owner ruling 2026-10-10: every card enters, bounded in total —
  // the interval shrinks with the count so the stagger tail stays
  // inside the 80ms the old three-child cap spent (§11 ~300ms).
  const gridStyle = {
    "--grid-stagger": `${entranceStaggerMs(items.length)}ms`,
  } as CSSProperties;
  return (
    <>
      <ul className="task-grid" aria-label="可领取的任务" style={gridStyle}>
        {items.map((card, index) => (
          <li
            key={card.id}
            style={{ "--card-index": index } as CSSProperties}
          >
            <TaskCard card={card} nowMs={now} />
          </li>
        ))}
      </ul>
      <div className="list-footer">
        {total !== null ? (
          <p className="list-count">
            已显示 {items.length} / {total} 个任务
          </p>
        ) : null}
        {canLoadMore ? (
          <Button
            variant="secondary"
            onClick={() => void loadMore()}
            disabled={loadingMore}
            aria-busy={loadingMore}
          >
            {loadingMore ? (
              <span className="spinner" aria-hidden="true" />
            ) : null}
            <span>加载更多</span>
          </Button>
        ) : null}
        {moreError !== null ? (
          <SectionError error={moreError} onRetry={() => void loadMore()} />
        ) : null}
      </div>
    </>
  );
}
