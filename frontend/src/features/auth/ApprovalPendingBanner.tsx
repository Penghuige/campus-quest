"use client";
/**
 * The PENDING_APPROVAL banner (auth-extension-II spec §2): an account
 * awaiting admin approval may log in and BROWSE, but every acting
 * endpoint answers the typed 403 APPROVAL_PENDING. This persistent
 * banner states that standing honestly — visible on every student page
 * (not a one-time toast), dismiss-free (the condition is the account's
 * real state, not a notice to acknowledge).
 *
 * The frozen-surface 403s render their own per-action copy through the
 * shared error envelopes (mobileLoginView's describeApprovalPending
 * maps the code); this banner is the ambient state, not the action
 * feedback.
 */
import { isPendingApproval } from "./mobileLoginView";
import type { MeProfile } from "./session";

export function ApprovalPendingBanner({ me }: { me: MeProfile }) {
  if (!isPendingApproval(me.status)) {
    return null;
  }
  return (
    <div className="alert alert-warning" role="status">
      <p>
        <span className="alert-marker" aria-hidden="true">!</span>
        <strong>账号待管理员审核。</strong>
        审核通过前你可以浏览任务、榜单与通知；领取、提交、兑换和评论将在审核通过后开放。
      </p>
    </div>
  );
}
