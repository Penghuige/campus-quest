/**
 * Shared form utilities for the innovation module (batch 1: talent;
 * batch 4 reuses them for recruitment). Extracted verbatim from the
 * draft form so the codepoint-count contract stays single-sourced:
 * lengths count UNICODE CODEPOINTS (the backend's rule), never UTF-16
 * code units — `🌱`.length is 2, Array.from gives 1.
 */
export function codePointCount(value: string): number {
  return Array.from(value).length;
}
