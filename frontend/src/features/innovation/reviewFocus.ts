export function restoreReviewFocus(opener: { element: HTMLElement; epoch: number } | null, fallback: HTMLElement | null, epoch: number): void {
  if (opener?.epoch !== epoch) return;
  const target = opener.element.isConnected && !opener.element.matches(":disabled") ? opener.element : fallback;
  if (target?.isConnected && !target.matches(":disabled")) target.focus();
}
