/**
 * Memory-only access-token manager (spec §5.6 auth model; PR #4
 * hardening Task 1).
 *
 * Session layout this module owns the client half of:
 * - the LONG-LIVED refresh token never touches JS — it lives only in the
 *   HttpOnly + Secure + SameSite=Lax cookie scoped to the auth paths;
 * - the SHORT-LIVED access token rides the login/refresh RESPONSE BODY
 *   and is kept HERE, in one module-level variable, for the tab's
 *   lifetime: `lib/api.ts` attaches it as `Authorization: Bearer …` on
 *   CampusQuest API requests. It is NEVER written to localStorage,
 *   sessionStorage, cookies, or any other persistent store — a page
 *   reload forgets it, and the refresh-cookie rotation below reissues
 *   it (the cold-start bootstrap).
 * - rotation is SINGLE-FLIGHT: concurrent 401 recoveries (several
 *   sections bootstrapping at once) share one in-flight `POST
 *   /auth/refresh`, so N concurrent callers cause exactly one cookie
 *   rotation — one refresh session, one rotation per wave, never a
 *   thundering herd against the rotate-once semantics.
 *
 * The rotation request authenticates by the HttpOnly cookie alone: no
 * `Authorization` header is attached (a stale bearer would be noise, and
 * the CSRF double-submit header is the mutation proof the endpoint's
 * guard wants when the cookie is presented).
 *
 * Server-side imports see an empty store: only browser flows call the
 * setters, so a Server Component reusing `apiRequest` simply sends no
 * bearer (the backend's 401 stays the authority there).
 */
import { readCsrfToken, CSRF_HEADER_NAME } from "./csrf";
import { observeServerDateHeader, currentServerClockOffset } from "./serverClock";
import { resolveApiPath } from "./apiBase";

/** The rotation endpoint (also the recursion guard for `lib/api.ts`). */
export const AUTH_REFRESH_PATH = "/api/v1/auth/refresh";

let accessToken: string | null = null;
let refreshInFlight: Promise<boolean> | null = null;
// Auth-transition window (final re-review P0): true between
// beginAuthTransition() and endAuthTransition() — no new refresh may
// start, and the epoch bump inside begin fences every older context's
// recovery. See beginAuthTransition for the cookie-ordering invariant.
let transitionActive = false;
// Auth-context epoch (final re-review P0): bumped by every EXPLICIT
// auth transition (login, logout) and untouched by refresh rotations.
// Requests capture the epoch they were sent under; a 401 landing in a
// LATER epoch means the human behind the tab changed (logout A ->
// login B) — replaying that request with the new account's token would
// execute A's intent against B's account, so it must never happen.
let authEpoch = 0;

/**
 * Record an explicit login: remember the token AND open a new auth
 * context (epoch bump). Rotation through the refresh cookie does NOT
 * bump — it is the same logged-in context renewing its credential.
 */
export function recordLogin(token: string): void {
  accessToken = token;
  authEpoch += 1;
}

/**
 * Record an explicit logout: forget the token AND close the auth
 * context (epoch bump), so in-flight requests from the closed context
 * can be distinguished from a same-session refresh.
 */
export function recordLogout(): void {
  accessToken = null;
  authEpoch += 1;
}

/** The auth-context epoch the caller's requests are running under. */
export function getAuthEpoch(): number {
  return authEpoch;
}

/** Remember a freshly issued access token WITHOUT opening a new context (rotation). */
export function setAccessToken(token: string): void {
  accessToken = token;
}

/** Forget the access token (logout; failed rotation). */
export function clearAccessToken(): void {
  accessToken = null;
}

/** The tab's current access token, or null when none is remembered. */
export function getAccessToken(): string | null {
  return accessToken;
}

/*
 * Cross-tab cold-start coordination (QA defect #1, owner ruling on the
 * PR #10 review): under strict rotate-once the invariant is ONE
 * POST /auth/refresh per cold-start wave — the second rotation kills
 * the first tab's just-minted token (the live-session row requires
 * `replaced_by IS NULL`).
 *
 * Mechanism (round-5 shape — probe/answer liveness):
 * 1. the round-trip runs inside a Web Locks request (same-origin tabs
 *    serialize on "cq:auth-refresh");
 * 2. the wave's LEADER (no adoption, no marker for our context) rotates
 *    once, then publishes — still under the lock — a NON-SECRET marker
 *    {waveId, contextId, at} (waveId is a fresh opaque uuid per wave)
 *    to localStorage and the mint {kind:"mint", waveId, token,
 *    contextId, sentAt, expiresAt} to the adoption BroadcastChannel,
 *    holding the lock a short delivery grace;
 * 3. a waiter seeing a marker for its context PROBES the wave — it
 *    posts {kind:"probe", waveId} and bounded-waits on the resolver-
 *    armed LONG-LIVED listener (a fresh channel cannot replay an
 *    already-posted message). Every live holder of the wave — any tab
 *    whose HELD credential still names it (the heldMint slot is written
 *    only when the tab TAKES a credential — its own rotation or an
 *    adoption it applied — never by observed channel traffic, so
 *    unrelated mints cannot make a holder forget its wave; round-8 P1)
 *    AND whose bearer is still USABLE (the mint carries the bearer's
 *    immutable expiry; a holder with an expired/near-expiry credential
 *    stays SILENT so the waiter becomes the refresh leader and
 *    publishes a genuinely new wave — round-6 P0) — ANSWERS by
 *    re-posting the mint with a RE-STAMPED
 *    sentAt (transport freshness only: a live holder answering IS the
 *    wave-liveness proof, but re-stamping never renews the CREDENTIAL;
 *    the answer's expiresAt is immutable). The waiter adopts with ZERO
 *    extra POSTs. Liveness is an active request/response, never a
 *    marker-age or mint-age guess;
 * 4. a probe timeout DECLARES THE WAVE FAILED — a bounded failure
 *    detector, NOT proof of death (round-6 P1: a live-but-suspended
 *    holder can miss the 400ms window). The contract this implements:
 *    - responsive same-origin participants keep the strict
 *      one-rotation-per-wave invariant;
 *    - a suspended holder MAY be superseded after the probe timeout;
 *    - that supersession is deliberate convergence, and the suspended
 *      holder self-heals on its next 401 (probe -> adopt the new wave,
 *      or rotate as the next leader). On timeout the waiter rotates as
 *    the NEW LEADER and the marker is replaced — this un-strands later
 *    cold starts (round-5 P1) and covers a same-tab reload over its
 *    OWN dead marker with no ownership counter to collide;
 * 5. wave identity is an OPAQUE uuid compared by EQUALITY (round-5
 *    P1a): per-tab numeric generations collide (every heap counts from
 *    0), and the numeric `<=` comparison misread a sibling's live wave
 *    as this tab's own dead one — retiring it.
 *
 * Divergence bound (accepted): a holder throttled past the probe
 * window (a deep-background tab) can miss a probe; its wave is retired
 * by the probing leader and SELF-HEALS on its next refresh (401 ->
 * probe -> adopt the new wave, or rotate as the next leader). Cost:
 * one extra rotation per incident — never a logout loop, never a
 * stranded tab.
 *
 * Cross-tab auth-context fence (rounds 1-3 P0): authEpoch is TAB-LOCAL
 * but cookies are origin-global, so a sibling's explicit login/logout
 * broadcasts a non-secret CONTEXT-RESET. Receivers drop the bearer,
 * bump the epoch, and notify app-level subscribers (onCrossTabAuthReset
 * — the session cache revalidates so mounted UI leaves the old account
 * BEFORE any new mutation can 401-refresh-retry as the new one).
 *
 * Supported baseline: Web Locks + BroadcastChannel (both evergreen).
 * No BroadcastChannel -> the probe cannot exist: a marker FAILS CLOSED
 * (surface false; never a speculative rotation) EXCEPT the wave THIS
 * tab published before a reload — the sessionStorage record (dies with
 * the tab) proves that holder dead. The server-side grace window
 * (PR #10, default off) is the non-browser/cross-origin backstop, not
 * a substitute.
 */

/** Web Locks name serializing rotations across same-origin tabs. */
const REFRESH_LOCK_NAME = "cq:auth-refresh";

/** How long a tab may WAIT for the lock before surfacing false. */
let refreshLockTimeoutMs = 10_000;

/** Test-only: shrink the cross-tab lock wait budget. */
export function setRefreshLockTimeoutForTests(ms: number): void {
  refreshLockTimeoutMs = ms;
}

/** Adoption notification channel (open listeners hear the mint immediately). */
const ADOPTION_CHANNEL = "cq-auth-adoption";
/** Cross-tab auth-context reset channel (login/logout fence). */
const CONTEXT_RESET_CHANNEL = "cq-auth-reset";
/**
 * localStorage handoff MARKER — NON-SECRET by construction (re-review
 * P1: the bearer NEVER touches persistent storage; the memory-only
 * contract is preserved). The marker only says "a mint for wave W of
 * context X was published at T and is reachable by probing the
 * adoption channel"; the bearer itself moves exclusively through
 * BroadcastChannel (memory, per-tab listeners). Written under the
 * lock, read under the lock — the deterministic lock-to-lock barrier.
 */
const HANDOFF_MARKER_KEY = "cq:auth-handoff-marker";
/**
 * sessionStorage: the waveId THIS tab last published. Survives reload,
 * dies with the tab — exactly the publisher's lifetime. Round-5 P1a:
 * identity is an OPAQUE UNIQUE id compared by equality, never a
 * per-tab counter (two tabs' counters collide; a shared uuid cannot).
 * Cleared by every explicit auth transition (ownership metadata must
 * not survive account changes).
 */
const PUBLISHED_WAVE_KEY = "cq:auth-published-wave";
// (Caveat, accepted inside the no-BC baseline: a DUPLICATED tab
// inherits sessionStorage, so the copy may treat the original's live
// wave as its own dead one. BroadcastChannel-capable contexts are
// unaffected — the probe decides there, not the record.)
/** A published mint older than this is not adoptable (wave-scale freshness). */
const ADOPTION_TTL_MS = 10_000;
/**
 * Bounded probe/answer window (round-5 P1b): the waiter posts the
 * probe and waits THIS long for a live holder to re-post the mint.
 * Exceeding it is a FAILURE-DETECTOR verdict — the wave is declared
 * failed and the caller rotates as the new leader — not proof of
 * death: only a responsive holder can answer inside it, a suspended
 * one may be superseded (see the contract in the header comment). A
 * few orders of magnitude above a same-origin BroadcastChannel
 * round-trip.
 */
const MINT_WAIT_MS = 400;
/**
 * Credential-boundary margin (round-6 P0): a bearer this close to its
 * exp is treated as spent — adopting or advertising it would buy one
 * doomed request. The JWT `exp` decode is ADVISORY (the server stays
 * the authority on every request); it only decides whether a credential
 * is OFFERED or ACCEPTED for handoff, never whether a request may run.
 */
const ADOPTION_EXPIRY_MARGIN_MS = 5_000;

interface AdoptionMessage {
  /** The wave this mint belongs to (probe/answer key; round-5 P1a). */
  waveId: string;
  token: string;
  /** The csrf_token from the SAME refresh response that minted the
   *  token (review P1a: never a post-response cookie read — the cookie
   *  is origin-global and a sibling could swap it mid-flight). */
  contextId: string | null;
  /**
   * TRANSPORT freshness (round-6 P0): when this mint was last posted.
   * A probe answer re-stamps THIS field only — it says the holder is
   * responsive, nothing about the credential.
   */
  sentAt: number;
  /**
   * CREDENTIAL validity boundary (round-6 P0): the bearer JWT's `exp`
   * in ms (advisory client-side decode; the server remains the
   * authority), or null when the token carries no decodable exp.
   * IMMUTABLE from minting onward — re-stamping sentAt never renews it.
   * Expired/near-expiry bearers are neither advertised nor adopted, so
   * an expired credential cannot be resurrected through the channel.
   */
  expiresAt: number | null;
}

// (Deploy note: the wire shape is versioned only by this unmerged PR —
// a hypothetical mixed round-5/round-6 pair would mutually reject each
// other's mints and trade one extra rotation per wave; deploys are
// atomic, so no mixed fleet can exist in production.)

/** The NON-SECRET marker (localStorage-safe: no bearer, no secret). */
interface HandoffMarker {
  /** Opaque unique wave identity — NOT a per-tab counter (round-5 P1a). */
  waveId: string;
  contextId: string | null;
  at: number;
}

/** Wire envelope on the adoption channel. */
type ChannelMessage =
  | ({ kind: "mint" } & AdoptionMessage)
  | { kind: "probe"; waveId: string };

/**
 * The latest OBSERVED channel mint (round-8 split): wake-up material for
 * armed waiters and the adoption-entry recheck — nothing more. Channel
 * traffic may replace it freely; it is NOT the tab's memory of what it
 * holds.
 */
let lastAdoption: AdoptionMessage | null = null;
/**
 * The credential THIS tab actually holds (round-8 P1): written only when
 * the tab TAKES a credential — its own rotation (publishMint) or an
 * adoption it applied — and cleared exactly when that credential dies
 * (context reset). Unrelated channel mints can never overwrite it, so a
 * responsive holder never forgets the wave it can answer probes for.
 */
let heldMint: AdoptionMessage | null = null;

/** This tab's id — reset broadcasts from OUR tab must not fence us
 *  (BroadcastChannel delivers to other channel instances in the SAME
 *  context; without this guard the tab that just logged in would drop
 *  its own fresh bearer). */
const TAB_ID =
  typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `tab-${Math.random().toString(36).slice(2)}`;

/** A fresh opaque wave identity (globally unique by construction). */
function freshWaveId(): string {
  return typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? `wave-${crypto.randomUUID()}`
    : `wave-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * Advisory decode of a bearer JWT's `exp` (round-6 P0) -> ms, or null.
 * NOT signature validation — the server stays the authority on every
 * request; this only decides whether a credential is OFFERED or
 * ACCEPTED for cross-tab handoff. Opaque tokens (no decodable exp)
 * return null and keep the handoff path that relies on the server's
 * 401 for recovery — CampusQuest's backend issues JWTs, so the
 * boundary is real in production.
 */
function bearerExpiryMs(token: string): number | null {
  const parts = token.split(".");
  if (parts.length !== 3) {
    return null;
  }
  try {
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
    const decoded =
      typeof atob === "function"
        ? atob(padded)
        : Buffer.from(padded, "base64").toString("utf8");
    const payload = JSON.parse(decoded) as { exp?: unknown };
    if (typeof payload.exp !== "number" || !Number.isFinite(payload.exp)) {
      return null;
    }
    if (payload.exp <= 0) {
      return 0; // literally-expired claim: never usable
    }
    const ms = payload.exp * 1000;
    return Number.isFinite(ms) ? ms : null;
  } catch {
    return null;
  }
}

/**
 * A handoff credential is USABLE while its decoded expiry is more than
 * the margin away, measured against the best SERVER-time estimate
 * (local clock + the observed response-Date offset; 0 until a header
 * has been seen). The exp is server-issued, so a slow local clock must
 * not vouch for a server-expired bearer (round-7 P2); the margin
 * absorbs the estimate's own error envelope (~1s header resolution
 * plus half an RTT) and residual skew. null = unknown expiry: such a
 * mint stays adoptable only while its ORIGINAL sentAt is TTL-fresh
 * (answerProbe never re-stamps it), so the TTL — not a vouch — bounds
 * unknown-expiry handoffs and any loop built on them.
 */
function bearerUsable(expiresAt: number | null): boolean {
  if (expiresAt === null) {
    return true;
  }
  return Date.now() + currentServerClockOffset() < expiresAt - ADOPTION_EXPIRY_MARGIN_MS;
}

/** sessionStorage record of the wave THIS tab published (or null). */
function readPublishedWave(): string | null {
  try {
    return typeof sessionStorage === "undefined"
      ? null
      : sessionStorage.getItem(PUBLISHED_WAVE_KEY);
  } catch {
    return null;
  }
}

function rememberPublishedWave(waveId: string): void {
  try {
    if (typeof sessionStorage !== "undefined") {
      sessionStorage.setItem(PUBLISHED_WAVE_KEY, waveId);
    }
  } catch {
    // best effort
  }
}

function forgetPublishedWave(): void {
  try {
    if (typeof sessionStorage !== "undefined") {
      sessionStorage.removeItem(PUBLISHED_WAVE_KEY);
    }
  } catch {
    // best effort
  }
}

interface WebLocksLike {
  request: (
    name: string,
    options: { signal?: AbortSignal },
    callback: () => Promise<boolean>,
  ) => Promise<boolean>;
}

function webLocks(): WebLocksLike | null {
  if (typeof navigator === "undefined") {
    return null;
  }
  const locks = (navigator as { locks?: unknown }).locks;
  return typeof locks === "object" && locks !== null &&
    typeof (locks as WebLocksLike).request === "function"
    ? (locks as WebLocksLike)
    : null;
}

/** Read the non-secret handoff marker (localStorage). */
function readMarker(): HandoffMarker | null {
  try {
    const raw =
      typeof localStorage === "undefined" ? null : localStorage.getItem(HANDOFF_MARKER_KEY);
    if (raw === null) {
      return null;
    }
    const parsed = JSON.parse(raw) as Partial<HandoffMarker> | null;
    if (
      parsed !== null &&
      typeof parsed.waveId === "string" &&
      parsed.waveId.length > 0 &&
      (parsed.contextId === null || typeof parsed.contextId === "string") &&
      typeof parsed.at === "number"
    ) {
      return {
        waveId: parsed.waveId,
        contextId: parsed.contextId ?? null,
        at: parsed.at,
      };
    }
    return null;
  } catch {
    return null;
  }
}

/** Write the marker — UNDER the lock, BEFORE its release. */
function writeMarker(marker: HandoffMarker): void {
  try {
    if (typeof localStorage !== "undefined") {
      localStorage.setItem(HANDOFF_MARKER_KEY, JSON.stringify(marker));
    }
  } catch {
    // Quota/private-mode: the channel mint still goes out; waiters
    // without a marker fall back to lastAdoption-or-fail-safe.
  }
}

function clearMarker(): void {
  try {
    if (typeof localStorage !== "undefined") {
      localStorage.removeItem(HANDOFF_MARKER_KEY);
    }
  } catch {
    // best effort
  }
}

/** Fire-and-forget post on the adoption channel (no listener kept). */
function postToAdoptionChannel(message: ChannelMessage): void {
  if (typeof BroadcastChannel === "undefined") {
    return;
  }
  const channel = new BroadcastChannel(ADOPTION_CHANNEL);
  channel.postMessage(message);
  channel.close();
}

/**
 * Resolve a marker's wave (round-5 P1b): LIVE — a holder answers the
 * probe with the mint (adopt, zero POSTs); DEAD — nobody answers within
 * the bounded window (the caller rotates as the new leader); or
 * UNKNOWABLE — no BroadcastChannel, so the probe cannot exist and only
 * this tab's own published-wave record can prove death.
 */
type WaveOutcome =
  | { kind: "mint"; message: AdoptionMessage }
  | { kind: "dead" }
  | { kind: "no-channel" };

function resolveWave(marker: HandoffMarker): Promise<WaveOutcome> {
  const valid = (message: AdoptionMessage): boolean =>
    message.waveId === marker.waveId &&
    message.sentAt >= marker.at - 50 &&
    message.contextId === marker.contextId &&
    // Re-stamped answers pass trivially; this bound only rejects a
    // replayed ORIGINAL broadcast arriving long after its wave (the
    // round-2 freshness pin, kept coherent with tryAdopt).
    Date.now() - message.sentAt < ADOPTION_TTL_MS &&
    // Credential boundary (round-6 P0): a handoff past (or within the
    // margin of) the bearer's immutable expiry is refused — transport
    // freshness must never mask credential expiry.
    bearerUsable(message.expiresAt);
  const already = lastAdoption;
  if (already !== null && valid(already)) {
    return Promise.resolve({ kind: "mint", message: already });
  }
  if (typeof BroadcastChannel === "undefined") {
    // Round-4 P1: browser-present + BroadcastChannel-absent IS the
    // unsupported case — the previous `&& window === undefined` guard
    // missed it and let a timer-only wait fall through to rotation.
    // Without a channel there is no probe, so death is provable ONLY
    // through this tab's own published-wave record; anything else
    // fails closed (never a speculative rotation).
    return Promise.resolve(
      readPublishedWave() === marker.waveId ? { kind: "dead" } : { kind: "no-channel" },
    );
  }
  return new Promise((resolve) => {
    let done = false;
    const finish = (value: WaveOutcome) => {
      if (done) {
        return;
      }
      done = true;
      pendingMintResolvers.delete(onMint);
      clearTimeout(timer);
      resolve(value);
    };
    const onMint = (message: AdoptionMessage) => {
      if (valid(message)) {
        finish({ kind: "mint", message });
      }
    };
    // Arm the long-lived-listener resolver, then PROBE the wave. The
    // arm-then-probe order covers every delivery window: a mint landing
    // after arming resolves through the resolver (the listener feeds
    // pendingMintResolvers), and no delivery can interleave between the
    // entry check and arming — that span is one synchronous task.
    pendingMintResolvers.add(onMint);
    postToAdoptionChannel({ kind: "probe", waveId: marker.waveId });
    const timer = setTimeout(() => finish({ kind: "dead" }), MINT_WAIT_MS);
  });
}

/**
 * Pending resolvers for the LONG-LIVED listener (round-3 P1a): a fresh
 * BroadcastChannel opened by a waiter cannot replay a message the
 * winner already posted, so resolveWave arms a resolver HERE and the
 * page-lifetime listener feeds it. Each resolver is wave-keyed THROUGH
 * ITS OWN VALIDATION and owns its lifecycle: an unrelated mint (another
 * wave, a queued message from a superseded one) is rejected by the
 * resolver's valid() check and leaves it ARMED — only the resolver's
 * own finish() (its wave's mint, or its timeout) removes it. Round-7
 * P1: a global clear after any mint let unrelated traffic disarm a
 * live wait, and the forced timeout rotated away a responsive wave.
 */
const pendingMintResolvers = new Set<(message: AdoptionMessage) => void>();

function feedAdoptionMessage(data: Partial<AdoptionMessage> | null): void {
  if (
    data !== null &&
    typeof data.waveId === "string" &&
    data.waveId.length > 0 &&
    typeof data.token === "string" &&
    data.token.length > 0 &&
    (data.contextId === null || typeof data.contextId === "string") &&
    typeof data.sentAt === "number" &&
    (data.expiresAt === null || typeof data.expiresAt === "number")
  ) {
    const message: AdoptionMessage = {
      waveId: data.waveId,
      token: data.token,
      contextId: data.contextId ?? null,
      sentAt: data.sentAt,
      expiresAt: data.expiresAt ?? null,
    };
    lastAdoption = message;
    // Offer the mint to every armed resolver; each keeps or drops ITSELF
    // per its own wave validation (no global clear — round-7 P1). The
    // HELD credential is deliberately untouched: observing an unrelated
    // mint may wake matching waiters, but must never make the tab
    // forget the wave it holds (round-8 P1).
    for (const resolve of pendingMintResolvers) {
      resolve(message);
    }
  }
}

/** Test seam: feed a mint as the long-lived listener would. */
export function deliverAdoptionForTests(message: AdoptionMessage): void {
  feedAdoptionMessage(message);
}

/**
 * Round-5 P1b: a live holder ANSWERS a probe by re-posting its mint —
 * the requester adopts with zero extra POSTs. Re-stamping sentAt is a
 * USABILITY VOUCH, so it is only given for a KNOWN expiry: an expired/
 * near-expiry bearer stays SILENT entirely (round-6 review P0 —
 * answering with a dead credential would resurrect it), and a mint
 * with NO decodable expiry is re-posted AS-IS, its original sentAt
 * intact — the receiver's TTL, not a vouch, bounds it (round-6
 * review: otherwise an unknown-expiry self-answer could re-stamp
 * itself fresh forever and the same-tab adopt loop would be unbounded
 * for opaque tokens). expiresAt is immutable on every path. The
 * receiver's contextId/waveId/expiry checks still bind the answer; a
 * dead-context holder cannot answer because the reset cleared its held
 * credential.
 */
function answerProbe(waveId: string): void {
  const held = heldMint;
  if (held !== null && held.waveId === waveId && bearerUsable(held.expiresAt)) {
    postToAdoptionChannel({
      kind: "mint",
      ...held,
      sentAt: held.expiresAt === null ? held.sentAt : Date.now(),
    });
  }
}

/** Dispatch one adoption-channel message (the long-lived listener). */
function onAdoptionChannelMessage(data: unknown): void {
  if (data === null || typeof data !== "object") {
    return;
  }
  const message = data as Partial<ChannelMessage>;
  if (message.kind === "mint") {
    feedAdoptionMessage(message);
  } else if (message.kind === "probe" && typeof message.waveId === "string") {
    answerProbe(message.waveId);
  }
}

// Sibling broadcasts may land while this tab still waits on the lock. The
// listener also carries the CONTEXT-RESET fence. Browser-only (`window`
// guard): in Node, BroadcastChannel exists but holds the event loop open.
if (typeof window !== "undefined" && typeof BroadcastChannel !== "undefined") {
  const listener = new BroadcastChannel(ADOPTION_CHANNEL);
  listener.onmessage = (event: MessageEvent) => {
    onAdoptionChannelMessage(event.data);
  };
  // Review P0: a sibling's EXPLICIT login/logout is an origin-global
  // auth-context change. Fence locally: drop the stale bearer, bump the
  // epoch (in-flight 401 recoveries from the OLD context can never
  // replay under the new account), AND notify app-level subscribers —
  // the session cache must not keep serving the OLD account's /me to
  // mounted UI (a stale-A click would otherwise 401-refresh-retry as B).
  const resetListener = new BroadcastChannel(CONTEXT_RESET_CHANNEL);
  resetListener.onmessage = (event: MessageEvent) => {
    const data = event.data as { from?: unknown } | null;
    if (data !== null && data.from === TAB_ID) {
      return; // our OWN transition already fenced us locally
    }
    accessToken = null;
    authEpoch += 1;
    lastAdoption = null;
    heldMint = null;
    notifyCrossTabReset();
  };
}

/**
 * Cross-tab auth-context reset subscriptions (round-3 P0): app-level
 * caches (the /me session cache first) subscribe so a SIBLING tab's
 * explicit login/logout is an application-level transition in THIS
 * tab too — mounted useSession consumers revalidate instead of
 * serving the previous account's cached UI.
 */
type ResetListener = () => void;
const resetListeners = new Set<ResetListener>();

/** Subscribe to cross-tab auth-context resets; returns an unsubscriber. */
export function onCrossTabAuthReset(listener: ResetListener): () => void {
  resetListeners.add(listener);
  return () => {
    resetListeners.delete(listener);
  };
}

function notifyCrossTabReset(): void {
  for (const listener of resetListeners) {
    listener();
  }
}

/**
 * Publish the mint: handoff slot first (the deterministic lock-to-lock
 * barrier), then the channel notification for already-open listeners.
 */
function publishMint(token: string, contextIdFromResponse: string | null): void {
  const message: AdoptionMessage = {
    waveId: freshWaveId(),
    token,
    contextId: contextIdFromResponse,
    sentAt: Date.now(),
    // Round-6 P0: the credential boundary rides the mint from birth —
    // advisory JWT `exp` decode, immutable thereafter.
    expiresAt: bearerExpiryMs(token),
  };
  lastAdoption = message;
  // This tab now OWNS the wave it just published — the held slot rides
  // with the credential (round-8 split).
  heldMint = message;
  // The MARKER is the only persistent artifact — non-secret (re-review
  // P1): waveId is an opaque uuid, contextId is the double-submit csrf
  // value already readable in a JS cookie, `at` is a timestamp. The
  // bearer never leaves memory.
  writeMarker({ waveId: message.waveId, contextId: message.contextId, at: message.sentAt });
  // Round-5 P1a: remember OUR wave by opaque identity (sessionStorage:
  // survives reload, dies with the tab — the publisher's exact
  // lifetime). Equality — never a counter comparison — is what proves a
  // later marker is this tab's OWN dead wave.
  rememberPublishedWave(message.waveId);
  postToAdoptionChannel({ kind: "mint", ...message });
}

/**
 * Review P0 fence sender: call at every EXPLICIT auth transition
 * (login/logout) after the transition request settles — tells sibling
 * tabs the origin's auth context changed.
 */
export function broadcastContextReset(): void {
  // The local fence applies EVERYWHERE (the sender is also a tab whose
  // stale handoff must die); the channel notification is browser-only.
  lastAdoption = null;
  heldMint = null;
  clearMarker();
  // Round-5 P1a: ownership metadata must not survive account changes —
  // the wave this tab published belongs to the CLOSED context.
  forgetPublishedWave();
  if (typeof window === "undefined" || typeof BroadcastChannel === "undefined") {
    return;
  }
  const channel = new BroadcastChannel(CONTEXT_RESET_CHANNEL);
  channel.postMessage({ reset: true, from: TAB_ID });
  channel.close();
}

/**
 * Immediate adoption check: the best VALID candidate for OUR context —
 * the HELD credential first (never forget what we own — round-8), then
 * the latest observed mint. Re-review P1: a superseded-wave candidate
 * is refused by the marker's wave identity regardless of TTL freshness
 * (round-5 review P2). Returns the message so the caller can promote
 * its adoption into the held slot.
 */
function tryAdopt(marker: HandoffMarker | null): AdoptionMessage | null {
  for (const candidate of [heldMint, lastAdoption]) {
    if (
      candidate !== null &&
      Date.now() - candidate.sentAt < ADOPTION_TTL_MS &&
      bearerUsable(candidate.expiresAt) &&
      candidate.contextId === readCsrfToken() &&
      (marker === null ||
        marker.contextId !== readCsrfToken() ||
        marker.waveId === candidate.waveId)
    ) {
      return candidate;
    }
  }
  return null;
}

/**
 * Rotate the session through the HttpOnly refresh cookie and remember
 * the new access token. Resolves `true` when a usable token came back.
 * Any failure (network, non-2xx, malformed body) forgets the stale
 * token and resolves `false` — the caller then surfaces its original
 * 401 (the session hook reads that as the anonymous state).
 *
 * SINGLE-FLIGHT: while one rotation is pending, every caller receives
 * the SAME promise; the slot frees only after it settles, so a later
 * 401 wave (requests that raced the rotation with the old token) may
 * legitimately start the next one.
 */
export function refreshAccessToken(): Promise<boolean> {
  // No NEW rotation may start while an explicit auth transition
  // (login/logout) is draining or executing: the transition's own
  // network request must be the LAST auth-cookie writer, and a refresh
  // started now could settle after it. Callers see `false` (their
  // original 401 surfaces) — correct, because their auth context is
  // being replaced anyway.
  if (transitionActive) {
    return Promise.resolve(false);
  }
  if (refreshInFlight === null) {
    refreshInFlight = rotate().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

async function rotate(): Promise<boolean> {
  // The auth context this rotation belongs to: a refresh that started
  // under one epoch and settles under another (its drain straddled a
  // beginAuthTransition) must not write the OLD context's token into
  // the NEW one's memory — and its HTTP response is still awaited by
  // the transition, so its Set-Cookie is applied BEFORE the login/
  // logout request goes out and loses the last-writer race by design.
  const epochAtStart = authEpoch;
  const locks = webLocks();
  if (locks === null) {
    return adoptOrRotate(epochAtStart);
  }
  // Serialize adopt-or-rotate across same-origin tabs. Rejection
  // handling is cause-aware:
  // - the wait TIMED OUT: firing unlocked would re-create the rotation
  //   race the lock exists to prevent — surface false; the next reload
  //   (or the winner's settled broadcast/cookie) recovers, which beats
  //   a guaranteed double-rotation under strict rotate-once;
  // - an explicit auth transition opened while we waited: its drain
  //   owns the cookie ordering — no new rotation — false;
  // - lock infrastructure refused BEFORE our callback ran: degrade to
  //   the unlocked adopt-or-rotate (performRotation is total).
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), refreshLockTimeoutMs);
  try {
    return await locks.request(
      REFRESH_LOCK_NAME,
      { signal: abort.signal },
      () => adoptOrRotate(epochAtStart),
    );
  } catch {
    if (abort.signal.aborted || transitionActive) {
      return false;
    }
    return adoptOrRotate(epochAtStart);
  } finally {
    clearTimeout(timer);
  }
}

/** The csrf_token riding the last successful refresh response (P1a). */
let lastMintContext: string | null | undefined = undefined;

/**
 * Inside the lock, in order:
 * 1. immediate adoption — the HELD credential first (never forget what
 *    we own), then the freshest observed mint (both wave-checked
 *    against the marker when one exists for our context; an applied
 *    adoption promotes into the held slot);
 * 2. wave resolution — a marker for our context routes to the
 *    PROBE/ANSWER exchange: a live holder with a USABLE bearer
 *    re-posts a re-stamped mint (adopt, zero POSTs); a timeout — a
 *    bounded failure-detector verdict, not proof of death — has this
 *    tab fall through as the new leader;
 * 3. fail-safe — no BroadcastChannel means no probe: surface FALSE
 *    (never a speculative second rotation into the strict rotate-once
 *    race), except this tab's OWN published wave (sessionStorage
 *    proves that holder dead — the reload-recovery unlock);
 * 4. otherwise this tab IS the wave's winner: rotate once, publish the
 *    marker + channel mint, and hold the lock a short delivery grace
 *    so the message is queued before the next waiter acquires.
 */
async function adoptOrRotate(epochAtStart: number): Promise<boolean> {
  if (authEpoch !== epochAtStart || transitionActive) {
    return false;
  }
  const marker = readMarker();
  const adopted = tryAdopt(marker);
  if (adopted !== null) {
    accessToken = adopted.token;
    heldMint = adopted;
    return true;
  }
  const markerIsOurs =
    marker !== null &&
    marker.contextId === readCsrfToken();
  if (marker !== null && markerIsOurs) {
    // Round-5: the wave's fate is decided by PROBE/ANSWER liveness, not
    // by marker age and not by any ownership counter. A live wave is
    // adopted from its holder's re-posted mint; a dead wave (probe
    // timeout, or — without a channel — this tab's own published-wave
    // record) lets this tab rotate as the new leader.
    const outcome = await resolveWave(marker);
    if (outcome.kind === "mint") {
      if (authEpoch !== epochAtStart || transitionActive) {
        return false;
      }
      accessToken = outcome.message.token;
      heldMint = outcome.message;
      return true;
    }
    if (outcome.kind === "no-channel") {
      return false;
    }
    // dead -> fall through: this tab is the new wave's leader, and
    // publishMint replaces the marker with the new wave.
  }
  // Wave LEADER (no adoption, no marker for our context): clear any
  // stale marker, rotate once, publish the new wave.
  if (marker !== null) {
    clearMarker();
  }
  const ok = await performRotation(epochAtStart);
  if (ok && accessToken !== null && lastMintContext !== undefined) {
    publishMint(accessToken, lastMintContext);
    // Delivery grace: keep the lock across a few macrotasks so the
    // mint is QUEUED for every waiter's bounded wait before release.
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  return ok;
}

/** The one network round-trip: POST /auth/refresh with the cookie pair. */
async function performRotation(epochAtStart: number): Promise<boolean> {
  try {
    const headers = new Headers({ Accept: "application/json" });
    const csrfToken = readCsrfToken();
    if (csrfToken !== null) {
      headers.set(CSRF_HEADER_NAME, csrfToken);
    }
    // resolveApiPath: under a path-mount deployment (NEXT_PUBLIC_API_BASE,
    // e.g. /campus) the refresh POST must carry the mount prefix like every
    // other API request — the bare root-relative path 404s there (found by
    // the production QA #1 re-test on the 207 deploy).
    const response = await fetch(resolveApiPath(AUTH_REFRESH_PATH), {
      method: "POST",
      headers,
      credentials: "include",
    });
    // Feed the shared clock estimate like every other API response.
    observeServerDateHeader(response.headers.get("Date"));
    if (!response.ok) {
      if (authEpoch === epochAtStart) {
        accessToken = null;
      }
      return false;
    }
    const body = (await response.json()) as {
      access_token?: unknown;
      csrf_token?: unknown;
    };
    if (typeof body.access_token !== "string" || body.access_token.length === 0) {
      if (authEpoch === epochAtStart) {
        accessToken = null;
      }
      return false;
    }
    if (authEpoch !== epochAtStart) {
      // The context this rotation served is gone (explicit logout or
      // login opened a new one while it was in flight): the drained
      // response's cookie side effect is ordered before the
      // transition's own request by construction; the MEMORY side
      // effect is simply dropped.
      return false;
    }
    accessToken = body.access_token;
    // P1a: the adoption label rides the SAME response — never a later
    // read of the origin-global cookie a sibling could have swapped.
    lastMintContext =
      typeof body.csrf_token === "string" && body.csrf_token.length > 0
        ? body.csrf_token
        : null;
    return true;
  } catch {
    // Network-level failure: no verdict on the session — forget the
    // stale token and let the caller's original error speak.
    if (authEpoch === epochAtStart) {
      accessToken = null;
    }
    return false;
  }
}

/**
 * Serialize an EXPLICIT auth transition (login/logout) against refresh
 * rotations (final re-review P0): mark the transition, bump the epoch
 * (requests from the closing context stop refreshing/replaying), and
 * DRAIN any in-flight refresh to completion BEFORE the caller sends
 * its own network request. Invariant: once the login/logout request
 * goes out, no older refresh response can still arrive in the future —
 * its Set-Cookie was already applied, so the transition's own cookies
  * are the last writers.
 */
export async function beginAuthTransition(): Promise<void> {
  transitionActive = true;
  authEpoch += 1;
  if (refreshInFlight !== null) {
    await refreshInFlight.catch(() => {
      // The drained rotation's own outcome is irrelevant here — what
      // mattered was ordering its response (and its Set-Cookie) ahead
      // of the transition request.
    });
  }
}

/** Close the transition window opened by beginAuthTransition. */
export function endAuthTransition(): void {
  transitionActive = false;
}

/** Test seam: reset the module singleton between test cases. */
export function resetAccessTokenManagerForTests(): void {
  accessToken = null;
  refreshInFlight = null;
  authEpoch = 0;
  transitionActive = false;
  lastAdoption = null;
  heldMint = null;
  lastMintContext = undefined;
}

/**
 * Test seam: the tab HOLDS this mint (its own adopted/published
 * credential) — sets both the held slot and the observed slot, exactly
 * as adopting the message would.
 */
export function receiveAdoptionForTests(message: AdoptionMessage): void {
  heldMint = message;
  lastAdoption = message;
}

/**
 * Test seam: run the browser reset listener's body (Node has no
 * `window`, so the receiver path never exists there) — drop the
 * bearer, bump the epoch, clear the adoption state, and notify
 * app-level subscribers exactly as a sibling broadcast would.
 */
export function receiveCrossTabResetForTests(): void {
  accessToken = null;
  authEpoch += 1;
  lastAdoption = null;
  heldMint = null;
  notifyCrossTabReset();
}

/**
 * Test seam: drive the long-lived adoption listener's dispatch (mint
 * feed + probe answering) exactly as the browser channel would.
 */
export function receiveChannelMessageForTests(data: unknown): void {
  onAdoptionChannelMessage(data);
}
