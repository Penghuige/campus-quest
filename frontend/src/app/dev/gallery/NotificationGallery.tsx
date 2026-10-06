"use client";
/**
 * Inbox-row × state fixtures for the dev gallery (plan-13 T4).
 *
 * The thin e2e world seeds ZERO notifications, so the inbox's
 * unread/read hierarchy, the per-category keylines (review / deadline /
 * redemption / system), the unknown-type degradation, the mark-read
 * failure line, and long-body truncation never render in a baselined
 * surface. This composition makes every state inspectable WITHOUT
 * touching world seeding (the T1–T3 gallery precedent): the REAL
 * exported NotificationRow + existing classes only, fully deterministic
 * fixtures (fixed ISO timestamps format through the pinned business
 * timezone; callbacks are no-ops).
 *
 * Client boundary: the real row renders the 标为已读 button with a
 * handler — neither crosses from the server gallery page.
 */
import type { NotificationItemDto } from "@/features/notifications/api";
import { inboxItemView } from "@/features/notifications/inboxView";
import { NotificationRow } from "@/features/notifications/NotificationInbox";
import { parseServerInstant } from "@/lib/time";

function itemFixture(overrides: Partial<NotificationItemDto>): NotificationItemDto {
  return {
    id: "00000000-0000-4000-8000-0000000000d1",
    event_type: "SUBMISSION_APPROVED",
    title: "提交审核通过",
    body: "恭喜，你的提交已通过审核，积分已到账。",
    read_at: null,
    created_at: "2027-01-15T02:00:00Z",
    ...overrides,
  };
}

const UNREAD_REVIEW = itemFixture({});

const UNREAD_DEADLINE = itemFixture({
  id: "00000000-0000-4000-8000-0000000000d2",
  event_type: "ASSIGNMENT_DEADLINE_24H",
  title: "任务截止提醒",
  body: "你领取的任务「校园关键词采集」将在 24 小时后截止，请记得按时提交。",
  created_at: "2027-01-14T09:30:00Z",
});

const UNREAD_REDEMPTION_REJECTED = itemFixture({
  id: "00000000-0000-4000-8000-0000000000d3",
  event_type: "REWARD_REDEMPTION_REJECTED",
  title: "兑换未通过",
  body: "你的「图书馆延时券」兑换未通过审核，积分已退回账户。",
  created_at: "2027-01-13T12:00:00Z",
});

const READ_REDEMPTION = itemFixture({
  id: "00000000-0000-4000-8000-0000000000d4",
  event_type: "REWARD_REDEMPTION_APPROVED",
  title: "兑换成功",
  body: "你的「打印额度券」兑换成功，请前往一卡通中心领取。",
  read_at: "2027-01-12T08:00:00Z",
  created_at: "2027-01-12T06:00:00Z",
});

const READ_SYSTEM = itemFixture({
  id: "00000000-0000-4000-8000-0000000000d5",
  event_type: "ACCOUNT_SECURITY",
  title: "账号安全提醒",
  body: "你的账号在新设备上登录。如非本人操作，请尽快修改密码。",
  read_at: "2027-01-11T08:00:00Z",
  created_at: "2027-01-11T07:00:00Z",
});

const UNKNOWN_TYPE = itemFixture({
  id: "00000000-0000-4000-8000-0000000000d6",
  event_type: "SOME_FUTURE_EVENT",
  title: "新类型通知",
  body: "尚未映射的事件类型降级为通用条目，行内容不丢失。",
  created_at: "2027-01-10T02:00:00Z",
});

const LONG_BODY = itemFixture({
  id: "00000000-0000-4000-8000-0000000000d7",
  title: "提交需要修改",
  event_type: "REVISION_REQUIRED",
  body:
    "审核意见：关键词覆盖范围偏窄，目前集中在一个子话题上，请按任务要求扩展到至少三个子话题。\n" +
    "另外，截图需要同时带上平台水印和采集时间，当前版本缺少时间信息，无法核对采集窗口。\n" +
    "第三段补充：修改后请直接重新提交，原领取记录保留，不需要重新领取任务；如有疑问可以在任务详情页的评论区提问，审核同学会在工作日内回复。",
  read_at: "2027-01-09T08:00:00Z",
  created_at: "2027-01-09T05:00:00Z",
});

const MARK_READ_FAILURE = {
  message: "服务暂时不可用，请稍后重试",
  requestId: null,
};

const noop = () => {};

function GalleryRow({
  item,
  markError = null,
}: {
  item: NotificationItemDto;
  markError?: Parameters<typeof NotificationRow>[0]["markError"];
}) {
  return (
    <NotificationRow
      view={inboxItemView(item, parseServerInstant)}
      item={item}
      markError={markError}
      onMarkRead={noop}
    />
  );
}

export function NotificationGallery() {
  return (
    <section className="section" aria-label="通知行">
      <h2 className="section-title">通知行 × 未读层级 × 事件类别</h2>
      <p className="progress-note">
        未读 = 字重 + 白色微浮面（无粗边框、无新徽章）；已读 = 安静。图标瓦片左侧描边区分事件类别：审核（墨蓝）/ 截止（琥珀）/ 兑换（青绿）/ 系统（信息蓝），文字标签恒在。
      </p>
      <ol className="notif-list">
        <GalleryRow item={UNREAD_REVIEW} />
        <GalleryRow item={UNREAD_DEADLINE} markError={MARK_READ_FAILURE} />
        <GalleryRow item={UNREAD_REDEMPTION_REJECTED} />
        <GalleryRow item={READ_REDEMPTION} />
        <GalleryRow item={READ_SYSTEM} />
      </ol>
      <p className="progress-note">
        类别与结果是两个通道：兑换未通过保留兑换类别描边 + 危险色结果底。未知事件类型降级为通用条目（系统类别），长正文三行截断。
      </p>
      <ol className="notif-list">
        <GalleryRow item={UNKNOWN_TYPE} />
        <GalleryRow item={LONG_BODY} />
      </ol>
    </section>
  );
}
