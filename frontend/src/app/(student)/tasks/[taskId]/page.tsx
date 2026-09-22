import type { Metadata } from "next";

import { CommentThread } from "@/features/community/CommentThread";
import { parseCommentSort } from "@/features/community/communityView";
import { TaskRating } from "@/features/community/TaskRating";
import { TaskDetailView } from "@/features/tasks/TaskDetailView";

export const metadata: Metadata = {
  title: "任务详情 · CampusQuest",
  description: "任务说明、奖励与截止信息，以及领取入口",
};

interface TaskDetailPageProps {
  params: Promise<{ taskId: string }>;
  searchParams: Promise<{ comments?: string | string[] }>;
}

/**
 * Task detail (patterns §8 archetype): dynamic segment -> detail
 * island, then the community surfaces — the comment thread directly
 * below the claim/detail view, the rating section after it. The
 * comment sort tab rides the URL (`?comments=latest|hot`, patterns
 * §4): the page parses it (garbage degrades to 最新) and REMOUNTS the
 * thread per tab so every fetch keys off the URL, never client state.
 */
export default async function TaskDetailPage({
  params,
  searchParams,
}: TaskDetailPageProps) {
  const [{ taskId }, query] = await Promise.all([params, searchParams]);
  const rawComments = query.comments;
  const sort = parseCommentSort(
    Array.isArray(rawComments) ? rawComments[0] : rawComments,
  );

  return (
    <>
      <TaskDetailView taskId={taskId} />
      {/* Keyed by task + sort: a navigation or tab switch REMOUNTS the
          islands so every fetch keys off the URL, never stale state. */}
      <CommentThread key={`${taskId}:${sort}`} taskId={taskId} sort={sort} />
      <TaskRating key={taskId} taskId={taskId} />
    </>
  );
}
