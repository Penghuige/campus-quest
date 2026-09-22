/**
 * Design-token bridge for non-CSS consumers (design-system §3 token-first).
 *
 * `globals.css` owns the token contract as CSS custom properties. The two
 * surfaces outside stylesheets that need token VALUES — the PWA manifest
 * (`app/manifest.ts`) and the browser-chrome `themeColor` in the root
 * layout — cannot read CSS variables at build time, so their source values
 * are mirrored HERE as the exact token strings. The unit test
 * `__tests__/pwa-manifest.test.ts` pins every string against the
 * `globals.css` text, so a token change that skips this module fails the
 * gate instead of silently drifting.
 *
 * Manifest/theme colors must be sRGB hex (web-app manifests are parsed
 * before any page CSS exists), so `oklchToHex` converts the OKLCH token
 * text itself — the reference OKLab -> linear sRGB -> gamma pipeline
 * (Björn Ottosson's spec matrices), anchored by unit tests at the
 * white/black extremes.
 */

/**
 * Raw `:root` token declarations mirrored from `src/app/globals.css`.
 * Keep the strings byte-identical to the CSS (the test enforces this).
 */
export const DESIGN_TOKENS = {
  /** `--primary` — the one clear accent; PWA theme_color. */
  primary: "oklch(52% 0.15 258)",
  /** `--background` — large page background; PWA background_color. */
  background: "oklch(97.5% 0.004 250)",
} as const;

/** Matches `oklch(<L>% <C> <H>)` with one optional leading `+`/`-` on H. */
const OKLCH_PATTERN =
  /^oklch\(\s*([0-9.]+)%\s+([0-9.]+)\s+(-?[0-9.]+)\s*\)$/;

/**
 * Convert one OKLCH token string to a clamped sRGB hex (`#rrggbb`).
 *
 * Throws `RangeError` on anything outside the token grammar — the only
 * inputs are the mirrored `DESIGN_TOKENS` strings, so a malformed value is
 * a contract break, not a color to guess at.
 */
export function oklchToHex(token: string): string {
  const match = OKLCH_PATTERN.exec(token);
  if (match === null) {
    throw new RangeError(`Not an oklch token: ${JSON.stringify(token)}`);
  }
  const lightness = Number(match[1]) / 100;
  const chroma = Number(match[2]);
  const hue = Number(match[3]);

  const radians = (hue * Math.PI) / 180;
  const a = chroma * Math.cos(radians);
  const b = chroma * Math.sin(radians);

  // OKLab -> LMS' (Ottosson's reference matrices).
  const lPrime = lightness + 0.3963377774 * a + 0.2158037573 * b;
  const mPrime = lightness - 0.1055613458 * a - 0.0638541728 * b;
  const sPrime = lightness - 0.0894841775 * a - 1.291485548 * b;
  const l = lPrime ** 3;
  const m = mPrime ** 3;
  const s = sPrime ** 3;

  // LMS -> linear sRGB.
  const linear = [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];

  const hex = linear
    .map((value) => {
      // Clamp before encoding; both mirrored tokens convert to
      // comfortably in-gamut sRGB, so no chroma-reduction step is needed.
      const clamped = Math.min(Math.max(value, 0), 1);
      const encoded =
        clamped <= 0.0031308
          ? 12.92 * clamped
          : 1.055 * clamped ** (1 / 2.4) - 0.055;
      return Math.round(encoded * 255)
        .toString(16)
        .padStart(2, "0");
    })
    .join("");
  return `#${hex}`;
}
