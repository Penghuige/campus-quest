/**
 * Typed wrappers for the student points/rewards endpoints (spec §15/§16;
 * backend `app/modules/points/router.py`). Shapes come from the GENERATED
 * OpenAPI types; the redemption-window verdict (`window_open`) is
 * computed server-side and is never re-derived here (patterns §3).
 */
import { apiRequest } from "@/lib/api";
import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];

/** `WalletResponse` — available / earned / spendable (spec §15.1/§16.2). */
export type WalletDto = Schemas["WalletResponse"];

/** `RewardItemResponse` — one enabled catalogue row with the window verdict. */
export type RewardItemDto = Schemas["RewardItemResponse"];

/** `RewardsListResponse`. */
export type RewardsListDto = Schemas["RewardsListResponse"];

export interface FetchInit {
  signal?: AbortSignal;
}

/** The caller's wallet strip (GET /api/v1/points/me). */
export function myWallet(init: FetchInit = {}): Promise<WalletDto> {
  return apiRequest<WalletDto>("/api/v1/points/me", { signal: init.signal });
}

/** The enabled reward catalogue (GET /api/v1/rewards). */
export function listRewards(init: FetchInit = {}): Promise<RewardsListDto> {
  return apiRequest<RewardsListDto>("/api/v1/rewards", { signal: init.signal });
}
