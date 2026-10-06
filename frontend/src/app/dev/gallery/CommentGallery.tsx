"use client";
/**
 * Comment-thread × state fixtures for the dev gallery (plan-13 T3).
 *
 * The seeded open task carries ZERO comments and the world's one
 * anonymous comment lives on task A, which no pixel-baseline shot
 * visits — so the thread's named/anonymous/edited/tombstone/
 * long-content rows, the echoed engagement controls, and the
 * anonymous-mode composer preview never render in a baselined surface.
 * This composition makes every state inspectable WITHOUT touching
 * world seeding (the T1/T2 gallery precedent): existing classes + the
 * REAL CommentRowItem / CommentVotes / CommentReactions /
 * CommentComposer only, fully deterministic fixtures (fixed timestamps
 * format through the pinned business timezone; the fixture-seed props
 * are gallery-only and the app never passes them).
 *
 * Client boundary: the real components render buttons with handlers
 * and read the session hook — neither crosses from the server gallery
 * page. The no-op callbacks live here.
 */
import { CommentComposer } from "@/features/community/CommentComposer";
import { CommentRowItem } from "@/features/community/CommentThread";
import { CommentReactions, CommentVotes } from "@/features/community/Reactions";
import type { CommentRowView } from "@/features/community/communityView";

function rowFixture(overrides: Partial<CommentRowView>): CommentRowView {
  return {
    id: "00000000-0000-4000-8000-0000000000c1",
    authorDisplay: "示例同学",
    isAnonymous: false,
    content:
      "提前两天完成的数据采集，关键词比想象中宽泛，建议先跑一小批试试水。",
    createdAtMs: 1_800_000_000_000,
    edited: false,
    deleted: false,
    ...overrides,
  };
}

const NAMED_ROOT = rowFixture({});

const ANONYMOUS_REPLY = rowFixture({
  id: "00000000-0000-4000-8000-0000000000c2",
  authorDisplay: "匿名用户",
  isAnonymous: true,
  content: "补充：预约座位尽量选靠窗的，网络更稳。",
  createdAtMs: 1_800_003_600_000,
  edited: true,
});

const LONG_CONTENT = rowFixture({
  id: "00000000-0000-4000-8000-0000000000c3",
  content:
    "这个任务我做了两遍才摸到门道。第一遍按照字面要求采了五十条，结果审核说关键词覆盖太窄，" +
    "集中在同一个子话题；第二遍换了个思路，先把词云铺开再逐类补齐，一次就过了。\n" +
    "另外截图的时候注意把时间和平台水印一起带上，审核会核对这两处，缺了会被打回重做。",
});

const TOMBSTONE_ROOT = rowFixture({
  id: "00000000-0000-4000-8000-0000000000c4",
  authorDisplay: "该评论已删除",
  content: null,
  deleted: true,
});

const TOMBSTONE_CHILD = rowFixture({
  id: "00000000-0000-4000-8000-0000000000c5",
  authorDisplay: "示例同学乙",
  content: "楼上说的方法亲测有效，补充一个避坑点：别在截止前一小时才提交。",
});

const noop = () => {};

export function CommentGallery() {
  return (
    <section className="section" aria-label="评论区">
      <h2 className="section-title">评论区行 × 状态</h2>
      <p className="progress-note">
        具名根评论 + 匿名 · 已编辑回复（二级缩进轨，回复保持视觉连接）。
      </p>
      <div className="comment-list">
        <div className="comment-thread">
          <CommentRowItem
            taskId="gallery-task"
            row={NAMED_ROOT}
            depth={1}
            replyTo={null}
            onReply={noop}
            onReport={noop}
            onPublished={noop}
          />
          <CommentRowItem
            taskId="gallery-task"
            row={ANONYMOUS_REPLY}
            depth={2}
            replyTo={null}
            onReply={noop}
            onReport={noop}
            onPublished={noop}
          />
        </div>
      </div>
      <p className="progress-note">
        长内容（多行 + 舒适阅读宽度）；已删除占位行更安静但可读，子评论留在缩进轨上。
      </p>
      <div className="comment-list">
        <div className="comment-thread comment-thread-leaf">
          <CommentRowItem
            taskId="gallery-task"
            row={LONG_CONTENT}
            depth={1}
            replyTo={null}
            onReply={noop}
            onReport={noop}
            onPublished={noop}
          />
        </div>
        <div className="comment-thread">
          <CommentRowItem
            taskId="gallery-task"
            row={TOMBSTONE_ROOT}
            depth={1}
            replyTo={null}
            onReply={noop}
            onReport={noop}
            onPublished={noop}
          />
          <CommentRowItem
            taskId="gallery-task"
            row={TOMBSTONE_CHILD}
            depth={2}
            replyTo={null}
            onReply={noop}
            onReport={noop}
            onPublished={noop}
          />
        </div>
      </div>

      <h2 className="section-title">互动控件 × 已回声状态</h2>
      <p className="progress-note">
        赞 / 表情：已按下 = 主色淡底，计数等宽数字（应用内由 POST 回声填充，此处为夹具种子）。
      </p>
      <div className="comment-actions">
        <CommentVotes
          commentId="00000000-0000-4000-8000-0000000000c1"
          initial={{ current_value: 1, likes: 3, dislikes: 1 }}
        />
        <CommentReactions
          commentId="00000000-0000-4000-8000-0000000000c1"
          initialCounts={{ "👍": 5, "🔥": 2 }}
          initialMine={["🔥"]}
        />
      </div>

      <h2 className="section-title">发布器 × 身份预览</h2>
      <p className="progress-note">
        默认公开昵称（V1 裁定）；匿名是显式选择，选中后预览行加重提示。
      </p>
      <CommentComposer taskId="gallery-task" onPublished={noop} />
      <CommentComposer
        taskId="gallery-task"
        onPublished={noop}
        initialMode="anonymous"
      />
    </section>
  );
}
