#!/usr/bin/env python3
"""Generate the project-owned PWA install icons (Plan 09 Task 7, brief
step 2) and the app favicon (Plan 09 Task 8 fold).

What this produces:
  frontend/public/icons/icon-192.png          192x192, purpose "any"
  frontend/public/icons/icon-512.png          512x512, purpose "any"
  frontend/public/icons/icon-maskable-512.png 512x512, purpose "maskable"
                                             (full-bleed, art inside the
                                             80% safe zone)
  frontend/src/app/icon.png                   64x64 favicon — the Next.js
                                             app-icon file convention
                                             (auto <link rel="icon">; kills
                                             the browser's /favicon.ico
                                             404 noted in the T7 report)

Recipe (deliberately dependency-light and deterministic):
- the mark is drawn at 4x supersampling and LANCZOS-downsampled: a
  rounded square in the shared `--primary` design token color with a
  white "CQ" monogram (the campus-quest brand initials), Pillow's
  scalable built-in font stroked in its own fill for weight — NO external
  image, font, or network asset is read;
- the color is the OKLCH `--primary` token string converted to sRGB hex
  by the same reference OKLab->sRGB pipeline as frontend
  src/lib/designTokens.ts (oklch(52% 0.15 258) -> #2a67bd), so icons,
  manifest theme_color, and app chrome stay one color;
- "maskable" art keeps everything inside the central 80% safe zone on a
  full-bleed square (the launcher mask crops the corners);
- the favicon is the SAME rounded-square mark as the "any" icons (no
  maskable full-bleed), small enough to stay crisp at 16/32px display.

Re-run from frontend/:  python3 scripts/generate-pwa-icons.py
Committed PNGs are build inputs — rerunning must be byte-stable (same
Pillow major version assumed).
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# oklch(52% 0.15 258) — the `--primary` token from src/app/globals.css.
PRIMARY_TOKEN = "oklch(52% 0.15 258)"
# --surface-1 (the icon's white mark sits on the primary fill).
MARK_COLOR = (255, 255, 255, 255)

SUPERSCALE = 4
FRONTEND_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = FRONTEND_DIR / "public" / "icons"
APP_ICON_PATH = FRONTEND_DIR / "src" / "app" / "icon.png"


def oklch_to_rgb(token: str) -> tuple[int, int, int]:
    """Reference OKLab -> linear sRGB -> gamma (mirrors designTokens.ts)."""
    parts = token.strip().removesuffix(")").split("(")[1].split()
    lightness = float(parts[0].removesuffix("%")) / 100
    chroma, hue = float(parts[1]), float(parts[2])
    rad = math.radians(hue)
    a, b = chroma * math.cos(rad), chroma * math.sin(rad)
    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_**3, m_**3, s_**3
    lin = (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )

    def enc(v: float) -> int:
        v = min(max(v, 0.0), 1.0)
        return round(
            (12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055) * 255
        )

    return enc(lin[0]), enc(lin[1]), enc(lin[2])


def fitted_font(target_height: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Builtin scalable font whose "CQ" cap height fits `target_height`."""
    size = target_height
    for _ in range(24):
        font = ImageFont.load_default(size=size)
        box = font.getbbox("CQ")
        if box[3] - box[1] >= target_height:
            break
        size = round(size * 1.12)
    return ImageFont.load_default(size=size)


def render(size: int, maskable: bool) -> Image.Image:
    big = size * SUPERSCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    fill = (*oklch_to_rgb(PRIMARY_TOKEN), 255)

    if maskable:
        # Full-bleed: the launcher mask crops whatever shape it wants.
        draw.rectangle([0, 0, big - 1, big - 1], fill=fill)
        mark_ratio = 0.38  # well inside the central 80% safe zone
    else:
        radius = round(big * 0.225)
        draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=radius, fill=fill)
        mark_ratio = 0.42

    target = round(big * mark_ratio)
    font = fitted_font(target)
    box = font.getbbox("CQ")
    text_w, text_h = box[2] - box[0], box[3] - box[1]
    stroke = max(2, big // 128)
    draw.text(
        ((big - text_w) / 2 - box[0], (big - text_h) / 2 - box[1]),
        "CQ",
        font=font,
        fill=MARK_COLOR,
        stroke_width=stroke,
        stroke_fill=MARK_COLOR,
    )
    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    APP_ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
    targets = [
        (OUT_DIR / "icon-192.png", 192, False),
        (OUT_DIR / "icon-512.png", 512, False),
        (OUT_DIR / "icon-maskable-512.png", 512, True),
        # The favicon (T8 fold): same rounded-square mark, 64x64 — sharp
        # at the 16/32px sizes browsers actually request.
        (APP_ICON_PATH, 64, False),
    ]
    for path, size, maskable in targets:
        render(size, maskable).save(path, format="PNG", optimize=True)
        print(f"wrote {path} ({size}x{size}, maskable={maskable})")


if __name__ == "__main__":
    main()
