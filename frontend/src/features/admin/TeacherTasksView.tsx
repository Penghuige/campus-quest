"use client";
/**
 * Teacher task workbench list (spec §41; patterns §8/§9; design §8
 * Teacher, §9 Tables): own + collaborated tasks, EVERY status including
 * DRAFT, one offset page at a time with load-more (server-backed
 * pagination; nothing filtered or counted client-side).
 *
 * Rows carry the lifecycle verbs (the pure `lifecycleActions` model —
 * confirm-on-destructive/publish) and the create dialog is the only
 * mutation besides them. After a transition the row updates FROM THE
 * SERVER VERDICT (`TaskTransitionResponse.status`), never a client
 * guess; a created DRAFT is prepended from the create echo.
 */
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  EmptyState,
  SectionError,
  SectionSkeleton,
} from "@/components/ui/sectionStates";
import { hasMorePages, mergeOffsetPage } from "@/lib/offsetPages";
import { formatDeadlineDateTime, parseServerInstant } from "@/lib/time";
import { rarityView } from "@/features/tasks/display";

import {
  listTeacherTasks,
  type TaskTransitionDto,
  type TeacherTaskDto,
  type TeacherTaskListItemDto,
} from "./teacherApi";
import { taskStatusView } from "./teacherView";
import { CreateTaskDialog } from "./CreateTaskDialog";
import { TaskLifecycleActions } from "./TaskLifecycleActions";

const PAGE_SIZE = 20;

export function TeacherTasksView() {
  const [items, setItems] = useState<TeacherTaskListItemDto[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<unknown>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [reloadSeed, setReloadSeed] = useState(0);

  useEffect(() => {
    // The effect only STARTS the fetch; setStates land in async callbacks.
    let cancelled = false;
    listTeacherTasks({ limit: PAGE_SIZE, offset: 0 }).then(
      (page) => {
        if (!cancelled) {
          setItems(page.items);
          setTotal(page.total);
          setPhase("ready");
        }
      },
      (cause: unknown) => {
        if (!cancelled) {
          setError(cause);
          setPhase("error");
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [reloadSeed]);

  const retry = useCallback(() => {
    setPhase("loading");
    setReloadSeed((seed) => seed + 1);
  }, []);

  const loadMore = useCallback(async () => {
    if (loadingMore) {
      return;
    }
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await listTeacherTasks({ limit: PAGE_SIZE, offset: items.length });
      setItems((previous) => mergeOffsetPage(previous, page.items, (row) => row.id));
      setTotal(page.total);
    } catch (cause) {
      // Non-fatal (the loaded rows stay usable) but never silent: the
      // inline message + retry render below the button (the T8 fold).
      setMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [items.length, loadingMore]);

  /** Apply a lifecycle verdict to its row (the response IS the new state). */
  const onTransitioned = useCallback((transition: TaskTransitionDto) => {
    setItems((previous) =>
      previous.map((item) =>
        item.id === transition.task_id
          ? { ...item, status: transition.status }
          : item,
      ),
    );
  }, []);

  /** A created DRAFT prepends from the create echo (server-confirmed). */
  const onCreated = useCallback((task: TeacherTaskDto) => {
    setItems((previous) => [task, ...previous]);
    setTotal((previous) => (previous === null ? 1 : previous + 1));
    setCreateOpen(false);
  }, []);

  return (
    <>
      <div className="section-head">
        <p className="list-count">
          {total !== null ? `共 ${total} 个任务（含协作）` : null}
        </p>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => setCreateOpen(true)}
        >
          新建任务
        </button>
      </div>

      {phase === "loading" ? (
        <SectionSkeleton lines={6} />
      ) : phase === "error" ? (
        <SectionError error={error} onRetry={retry} retryLabel="重新加载" />
      ) : items.length === 0 ? (
        <EmptyState
          title="还没有任务"
          hint="创建第一个任务草稿，导入任务单元后即可发布给学生领取"
        >
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => setCreateOpen(true)}
          >
            新建任务
          </button>
        </EmptyState>
      ) : (
        <>
          <div className="table-scroll">
            <table className="staff-table" aria-label="我的任务（含协作）">
              <thead>
                <tr>
                  <th scope="col">标题</th>
                  <th scope="col">状态</th>
                  <th scope="col">稀有度</th>
                  <th scope="col">基础积分</th>
                  <th scope="col">截止</th>
                  <th scope="col">创建时间</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <TaskRow
                    key={item.id}
                    item={item}
                    onTransitioned={onTransitioned}
                  />
                ))}
              </tbody>
            </table>
          </div>

          <div className="list-footer">
            {total !== null ? (
              <p className="list-count">
                已显示 {items.length} / {total} 个任务
              </p>
            ) : null}
            {hasMorePages(items.length, total ?? 0) ? (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => void loadMore()}
                disabled={loadingMore}
                aria-busy={loadingMore}
              >
                {loadingMore ? <span className="spinner" aria-hidden="true" /> : null}
                <span>加载更多</span>
              </button>
            ) : null}
            {moreError !== null ? (
              <SectionError error={moreError} onRetry={() => void loadMore()} />
            ) : null}
          </div>
        </>
      )}

      <CreateTaskDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={onCreated}
      />
    </>
  );
}

function TaskRow({
  item,
  onTransitioned,
}: {
  item: TeacherTaskListItemDto;
  onTransitioned: (transition: TaskTransitionDto) => void;
}) {
  const status = taskStatusView(item.status);
  const rarity = rarityView(item.rarity);
  const createdMs = parseServerInstant(item.created_at);
  // A management list needs the absolute deadline, not a countdown
  // (patterns §14); urgency hints live on the student surface.
  const deadlineText =
    item.deadline_mode === "RELATIVE"
      ? item.duration_minutes !== null
        ? `领取后 ${item.duration_minutes} 分钟`
        : "领取后限时"
      : item.fixed_deadline_at !== null
        ? formatDeadlineDateTime(parseServerInstant(item.fixed_deadline_at))
        : "截止时间待定";
  return (
    <tr>
      <td className="staff-cell-title">
        <Link className="link" href={`/teacher/tasks/${encodeURIComponent(item.id)}`}>
          {item.title}
        </Link>
      </td>
      <td>
        <span className={`badge badge-${status.tone}`}>{status.label}</span>
      </td>
      <td>
        <span className="rarity-badge" data-rarity={rarity.rarity}>
          {rarity.label}
        </span>
      </td>
      <td className="meta-num">{item.base_reward_points}</td>
      <td>{deadlineText}</td>
      <td>{formatDeadlineDateTime(createdMs)}</td>
      <td>
        <TaskLifecycleActions
          taskId={item.id}
          status={item.status}
          onTransitioned={onTransitioned}
          size="compact"
        />
      </td>
    </tr>
  );
}
