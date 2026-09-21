/**
 * Typed wrappers for the rankings/growth endpoints (spec §17 boards /
 * 我的附近, §19 growth; backend `app/modules/rankings/router.py`).
 *
 * The PERIOD is a server decision — daily/monthly address the CURRENT
 * business day/month derived from the server clock; the client only
 * picks a board endpoint (`period` on around-me is a view choice, not a
 * period computation). `limit` is the board size, not pagination.
 */
import { apiRequest } from "@/lib/api";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];

/** `BoardResponse` — top entries plus the caller's own standing. */
export type BoardDto = Schemas["BoardResponse"];

/** `RankingEntryResponse` — EXACTLY nickname/honor/score/rank (§17/§40). */
export type RankingEntryDto = Schemas["RankingEntryResponse"];

/** `AroundMeResponse` — the caller's neighborhood with GLOBAL ranks. */
export type AroundMeDto = Schemas["AroundMeResponse"];

/** `GrowthResponse` — the §19 personal growth profile. */
export type GrowthDto = Schemas["GrowthResponse"];

/** `OwnedHonorResponse` — one honor the caller owns. */
export type OwnedHonorDto = Schemas["OwnedHonorResponse"];

/** The board names the backend accepts (rankings router `_PERIOD_PATTERN`). */
export type RankingPeriodKey = "daily" | "monthly" | "all";

/** The server's default board size (rankings router `DEFAULT_BOARD_LIMIT`). */
export const DEFAULT_BOARD_LIMIT = 20;

/** The server's default around-me radius (router `DEFAULT_AROUND_RADIUS`). */
export const DEFAULT_AROUND_RADIUS = 5;

export interface FetchInit {
  signal?: AbortSignal;
}

/** The current business month's board (GET /api/v1/rankings/monthly). */
export function monthlyBoard(
  limit = DEFAULT_BOARD_LIMIT,
  init: FetchInit = {},
): Promise<BoardDto> {
  return apiRequest<BoardDto>(`/api/v1/rankings/monthly?limit=${limit}`, {
    signal: init.signal,
  });
}

/** Today's business-day board (GET /api/v1/rankings/daily). */
export function dailyBoard(
  limit = DEFAULT_BOARD_LIMIT,
  init: FetchInit = {},
): Promise<BoardDto> {
  return apiRequest<BoardDto>(`/api/v1/rankings/daily?limit=${limit}`, {
    signal: init.signal,
  });
}

/** The all-time board (GET /api/v1/rankings/all). */
export function allTimeBoard(
  limit = DEFAULT_BOARD_LIMIT,
  init: FetchInit = {},
): Promise<BoardDto> {
  return apiRequest<BoardDto>(`/api/v1/rankings/all?limit=${limit}`, {
    signal: init.signal,
  });
}

/** One board by period key (the tab -> endpoint dispatch). */
export function boardForPeriod(
  period: RankingPeriodKey,
  limit = DEFAULT_BOARD_LIMIT,
  init: FetchInit = {},
): Promise<BoardDto> {
  if (period === "daily") {
    return dailyBoard(limit, init);
  }
  if (period === "monthly") {
    return monthlyBoard(limit, init);
  }
  return allTimeBoard(limit, init);
}

/**
 * The caller's neighborhood on one named board
 * (GET /api/v1/rankings/around-me?period=&radius=), numbered with GLOBAL
 * ranks; a caller with no score gets an empty window.
 */
export function aroundMeBoard(
  period: RankingPeriodKey,
  radius = DEFAULT_AROUND_RADIUS,
  init: FetchInit = {},
): Promise<AroundMeDto> {
  return apiRequest<AroundMeDto>(
    `/api/v1/rankings/around-me?period=${period}&radius=${radius}`,
    { signal: init.signal },
  );
}

/** The §19 personal growth profile (GET /api/v1/growth/me). */
export function myGrowth(init: FetchInit = {}): Promise<GrowthDto> {
  return apiRequest<GrowthDto>("/api/v1/growth/me", { signal: init.signal });
}
