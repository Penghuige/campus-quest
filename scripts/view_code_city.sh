#!/usr/bin/env bash
# Serve the CodeCharta 3D code city in a browser — no Electron required.
#
# Why this exists: the codecharta-visualization desktop app's postinstall
# downloads a ~100MB Electron binary from GitHub, which fails or times out
# on slow/blocked links (observed 2026-10-17), and needs DISPLAY set when
# launched from tmux. The package's web build serves the identical UI
# statically; the ?file= URL parameter loads the map without the in-app
# file dialog.
#
# Usage:  scripts/view_code_city.sh [port]     (default port 8190)
# Before: make arch-city                       (produces build/code-city.cc.json)
# Stop:   Ctrl+C
# Port note: keep off 3000/8000/8100 (campusquest e2e stack protocol).
set -euo pipefail

PORT="${1:-8190}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CITY="$ROOT/build/code-city.cc.json"

if [ ! -f "$CITY" ]; then
    echo "missing $CITY — run 'make arch-city' first" >&2
    exit 1
fi

DIST="$(find "$HOME/.npm/_npx" -type d -path '*codecharta-visualization/dist/bundler/browser' 2>/dev/null | head -1 || true)"
if [ -z "$DIST" ] || [ ! -f "$DIST/index.html" ]; then
    echo "codecharta-visualization web assets not in the npx cache; fetching (electron mirror for slow links)..." >&2
    ELECTRON_MIRROR="${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/}" \
        npx -y codecharta-visualization --version >/dev/null 2>&1 || true
    DIST="$(find "$HOME/.npm/_npx" -type d -path '*codecharta-visualization/dist/bundler/browser' 2>/dev/null | head -1 || true)"
fi
if [ -z "$DIST" ] || [ ! -f "$DIST/index.html" ]; then
    echo "could not locate the visualization web assets; install once with:" >&2
    echo "  ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ npx -y codecharta-visualization --version" >&2
    exit 1
fi

mkdir -p "$ROOT/build/cc-web"
cp -r "$DIST/." "$ROOT/build/cc-web/"
cp "$CITY" "$ROOT/build/cc-web/"

echo
echo "  open:  http://localhost:$PORT/?file=code-city.cc.json"
echo "  hint:  Metric panel -> Height = max_complexity_per_function, Color = number_of_commits"
echo "         (tall + hot buildings are the refactoring hotspots)"
echo "  stop:  Ctrl+C"
echo
cd "$ROOT/build/cc-web"
exec python3 -m http.server "$PORT"
