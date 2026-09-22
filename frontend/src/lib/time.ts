/**
 * Deadline and countdown helpers (spec §9, patterns §14).
 *
 * SERVER TIME IS AUTHORITATIVE:
 * - instants arrive as backend UTC ISO-8601 strings; `parseServerInstant`
 *   turns them into epoch milliseconds;
 * - a client clock can drift, so countdown math should run against
 *   `serverNow(offset)` using an offset estimated from response `Date`
 *   headers (`estimateServerClockOffset`);
 * - any result here is presentation-only. At a deadline boundary the
 *   frontend refetches the authoritative Claim state (spec §9.3); it never
 *   lets a local countdown grant or deny a submission.
 *
 * Display formatting goes through Intl with the `BusinessTimeConfig`
 * placeholder below (BUSINESS_TIMEZONE-shaped: the backend runs
 * `BUSINESS_TIMEZONE=Asia/Shanghai`); timezone arithmetic is done by the
 * Intl timezone database, never by hardcoded `+8` offsets. When a runtime
 * configuration source lands, swap the constant — the functions already
 * take the config as a parameter.
 *
 * Every function is pure (no `Date.now()` hidden inside; callers pass
 * `now`), so countdowns can be driven by a single ticking clock in a
 * component and tested deterministically.
 */

/** Display locale + business timezone (placeholder for BUSINESS_TIMEZONE). */
export interface BusinessTimeConfig {
  locale: string;
  timeZone: string;
}

export const BUSINESS_TIME_CONFIG: BusinessTimeConfig = {
  locale: "zh-CN",
  timeZone: "Asia/Shanghai",
};

/**
 * Parse a backend UTC instant ("2026-09-19T12:00:00Z" or "+00:00" form)
 * into epoch milliseconds. Throws `RangeError` on anything the backend
 * would never send — a malformed timestamp is a contract break, not a
 * deadline of zero.
 */
export function parseServerInstant(iso: string): number {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) {
    throw new RangeError(`Not a server ISO instant: ${JSON.stringify(iso)}`);
  }
  return ms;
}

/**
 * Estimate the client-server clock offset from an HTTP `Date` response
 * header.
 *
 * Method (documented contract): `offset = Date(header) - receivedAt`, the
 * milliseconds to ADD to the local clock to approximate server time. The
 * HTTP `Date` header has one-second resolution and is written when the
 * response starts, so without RTT measurement the estimate runs late by
 * roughly half the round trip and ±1s of header granularity. That is ample
 * for human-facing countdown text ("还剩 3 小时 12 分") and useless for
 * enforcement — enforcement is the backend's job (spec §9.3).
 *
 * Returns `null` when the header is missing or unparseable; callers keep
 * offset 0.
 */
export function estimateServerClockOffset(
  dateHeader: string | null,
  receivedAtMs: number,
): number | null {
  if (dateHeader === null) {
    return null;
  }
  const serverMs = Date.parse(dateHeader);
  if (Number.isNaN(serverMs)) {
    return null;
  }
  return serverMs - receivedAtMs;
}

/** Current server-side time given a clock offset (default 0). */
export function serverNow(offsetMs = 0): number {
  return Date.now() + offsetMs;
}

export interface CountdownParts {
  /** Deadline minus now, in ms; <= 0 means expired. */
  totalMs: number;
  expired: boolean;
  days: number;
  hours: number;
  minutes: number;
  seconds: number;
}

/** Decompose the distance to `deadlineMs` (both epoch ms), pure. */
export function countdownFrom(deadlineMs: number, nowMs: number): CountdownParts {
  const totalMs = deadlineMs - nowMs;
  const expired = totalMs <= 0;
  const absolute = Math.max(Math.abs(totalMs), 0);
  const totalSeconds = Math.floor(absolute / 1000);
  return {
    totalMs,
    expired,
    days: Math.floor(totalSeconds / 86_400),
    hours: Math.floor((totalSeconds % 86_400) / 3_600),
    minutes: Math.floor((totalSeconds % 3_600) / 60),
    seconds: totalSeconds % 60,
  };
}

/**
 * Relative deadline text: "3 天 4 小时" / "3 小时 12 分" / "45 秒" /
 * "不到 1 分钟" / "已截止" — the two most significant units (patterns §14
 * example format). Unit labels cover the zh-CN placeholder config plus an
 * `en` fallback; `config` selects between them.
 */
export function formatCountdownText(
  deadlineMs: number,
  nowMs: number,
  config: BusinessTimeConfig = BUSINESS_TIME_CONFIG,
): string {
  const parts = countdownFrom(deadlineMs, nowMs);
  if (parts.expired) {
    return label(config, { zh: "已截止", en: "Closed" });
  }
  const nf = new Intl.NumberFormat(config.locale);
  const units = labels(config);
  if (parts.days > 0) {
    return `${nf.format(parts.days)} ${units.day} ${nf.format(parts.hours)} ${units.hour}`;
  }
  if (parts.hours > 0) {
    return `${nf.format(parts.hours)} ${units.hour} ${nf.format(parts.minutes)} ${units.minute}`;
  }
  if (parts.minutes > 0) {
    return `${nf.format(parts.minutes)} ${units.minute} ${nf.format(parts.seconds)} ${units.second}`;
  }
  return label(config, { zh: "不到 1 分钟", en: "less than a minute" });
}

/**
 * Absolute deadline text in the business timezone, e.g. "9月19日 18:00"
 * (spec §14 example). Year is omitted; add a variant when a view needs it.
 */
export function formatDeadlineDateTime(
  instantMs: number,
  config: BusinessTimeConfig = BUSINESS_TIME_CONFIG,
): string {
  return new Intl.DateTimeFormat(config.locale, {
    timeZone: config.timeZone,
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(instantMs));
}

/**
 * Combined display line, e.g. "9月19日 18:00 · 还剩 3 小时 12 分"
 * (patterns §14). Pure derivation; never authoritative.
 */
export function formatDeadlineSummary(
  deadlineMs: number,
  nowMs: number,
  config: BusinessTimeConfig = BUSINESS_TIME_CONFIG,
): string {
  const absolute = formatDeadlineDateTime(deadlineMs, config);
  const relative = formatCountdownText(deadlineMs, nowMs, config);
  if (countdownFrom(deadlineMs, nowMs).expired) {
    return `${absolute} · ${relative}`;
  }
  return `${absolute} · ${label(config, { zh: "还剩", en: "Time left" })} ${relative}`;
}

function label(
  config: BusinessTimeConfig,
  strings: { zh: string; en: string },
): string {
  return config.locale.toLowerCase().startsWith("zh") ? strings.zh : strings.en;
}

function labels(config: BusinessTimeConfig): {
  day: string;
  hour: string;
  minute: string;
  second: string;
} {
  const zh = config.locale.toLowerCase().startsWith("zh");
  return zh
    ? { day: "天", hour: "小时", minute: "分", second: "秒" }
    : { day: "d", hour: "h", minute: "m", second: "s" };
}
