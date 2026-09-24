/**
 * Claim progress strip (Plan 11 brief §9): the claim/submission state
 * machine made visible — 领取 → 提交 → 校验 → 审核 → 完成 with the
 * current step strong, previous steps quiet, future steps subtle.
 *
 * Pure presentation of `claimStepView` (display.ts); the status badge
 * beside the title keeps carrying the exact backend status label.
 */
import { claimStepView } from "@/features/tasks/display";

export function ClaimSteps({ status }: { status: string }) {
  const view = claimStepView(status);
  if (!view.linear) {
    return null;
  }
  return (
    <ol className="claim-steps" aria-label="进度">
      {view.steps.map((step) => (
        <li key={step.label} className="claim-step" data-state={step.state}>
          <span className="claim-step-dot" aria-hidden="true" />
          <span className="claim-step-label">{step.label}</span>
        </li>
      ))}
    </ol>
  );
}
