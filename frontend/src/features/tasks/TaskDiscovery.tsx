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
 */
import { useCallback, useEffect, useState } from "react";

import {
  EmptyState,
  SectionCardsSkeleton,
  SectionError,
} from "@/components/ui/sectionStates";

import { listTasks, type TaskCardDto, type TaskListPageDto } from "./api";
import { TaskCard } from "./TaskCard";
import { useNow } from "./useNow";

const PAGE_SIZE = 12;

export function TaskDiscovery() {
  // Minute-granularity text; the card countdown is display-only (§42).
  const now = useNow(60_000);
  const [items, setItems] = useState<TaskCardDto[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);

  const fetchFirstPage = useCallback(
    () => listTasks({ limit: PAGE_SIZE, offset: 0 }),
    [],
  );

  const applyPage = useCallback((page: TaskListPageDto) => {
    setItems(page.items);
    setTotal(page.total);
    setPhase("ready");
  }, []);

  const applyError = useCallback((cause: unknown) => {
    setError(cause);
    setPhase("error");
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchFirstPage().then(
      (page) => {
        if (!cancelled) {
          applyPage(page);
        }
      },
      (cause: unknown) => {
        if (!cancelled) {
          applyError(cause);
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [fetchFirstPage, applyPage, applyError]);

  const retry = useCallback(() => {
    setPhase("loading");
    fetchFirstPage().then(applyPage, applyError);
  }, [fetchFirstPage, applyPage, applyError]);

  const loadMore = useCallback(async () => {
    if (loadingMore) {
      return;
    }
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await listTasks({ limit: PAGE_SIZE, offset: items.length });
      setItems((previous) => [...previous, ...page.items]);
      setTotal(page.total);
    } catch (cause) {
      setMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [items.length, loadingMore]);

  if (phase === "loading") {
    return <SectionCardsSkeleton cards={6} />;
  }
  if (phase === "error") {
    return <SectionError error={error} onRetry={retry} retryLabel="重新加载" />;
  }
  if (items.length === 0) {
    return (
      <EmptyState
        title="暂无可领取的任务"
        hint="老师还没有发布可领取的任务，稍后再来看看"
      />
    );
  }

  const canLoadMore = total !== null && items.length < total;
  return (
    <>
      <ul className="task-grid" aria-label="可领取的任务">
        {items.map((card) => (
          <li key={card.id}>
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
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => void loadMore()}
            disabled={loadingMore}
            aria-busy={loadingMore}
          >
            {loadingMore ? (
              <span className="spinner" aria-hidden="true" />
            ) : null}
            <span>加载更多</span>
          </button>
        ) : null}
        {moreError !== null ? (
          <SectionError error={moreError} onRetry={() => void loadMore()} />
        ) : null}
      </div>
    </>
  );
}
