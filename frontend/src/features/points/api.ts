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

/** `RedemptionResponse` — the student's view of one redemption (§16.1). */
export type RedemptionDto = Schemas["RedemptionResponse"];

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

/**
 * Request one redemption (POST /rewards/{reward_id}/redeem, 201): the
 * server freezes the points and pre-occupies the stock unit atomically;
 * every gate failure answers a typed 409/422 envelope.
 *
 * `Idempotency-Key` is ADVISORY in V1 (spec §32; backend points/router
 * module docstring): the header is sent from day one so clients are
 * shaped for the audited store, but the backend does NOT dedupe on it —
 * the caller therefore mints a FRESH key per attempt rather than
 * replaying one (a retry after a typed conflict is a new logical
 * attempt; replay-stable dedupe arrives with the idempotency store).
 */
export function redeemReward(
  rewardId: string,
  idempotencyKey: string,
): Promise<RedemptionDto> {
  return apiRequest<RedemptionDto>(
    `/api/v1/rewards/${encodeURIComponent(rewardId)}/redeem`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
}
