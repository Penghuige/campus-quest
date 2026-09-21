/**
 * Typed wrapper for the rankings board the student dashboard reads (spec
 * §17; backend `app/modules/rankings/router.py`). The period is a SERVER
 * decision — the client only chooses the board endpoint; limit is the
 * board size, not pagination.
 */
import { apiRequest } from "@/lib/api";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];

/** `BoardResponse` — top entries plus the caller's own standing. */
export type BoardDto = Schemas["BoardResponse"];

export interface FetchInit {
  signal?: AbortSignal;
}

/** The current business month's board (GET /api/v1/rankings/monthly). */
export function monthlyBoard(
  limit = 5,
  init: FetchInit = {},
): Promise<BoardDto> {
  return apiRequest<BoardDto>(`/api/v1/rankings/monthly?limit=${limit}`, {
    signal: init.signal,
  });
}
