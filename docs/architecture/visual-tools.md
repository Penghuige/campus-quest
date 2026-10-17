# Architecture observability tools

Three layers, all derived from the tree — nothing here is hand-maintained.

## 1. Committed map (always current with the repo)

`docs/architecture/as-built.md` — module dependency graphs (mermaid,
GitHub-rendered) + boundary-debt tables for both trees.

- Regenerate: `make arch-map` (commit the result with changes that drift it)
- Freshness check: `cd backend && uv run python ../scripts/generate_as_built.py --check`

## 2. Contract gate (CI-enforced drift guard)

`make arch-lint` — import-linter contracts (`backend/pyproject.toml
[tool.importlinter]`) + dependency-cruiser boundaries
(`frontend/.dependency-cruiser.cjs`). Wired into `make verify`,
`make release-gate` (step 0), and ci.yml. Known drift is waived in-config
(rental-style); the as-built map reports the same drift without waivers.

## 3. Local interactive visuals (owner-facing, not committed)

### CodeGraphContext (code knowledge graph; also serves agents)

- One-time: `uv tool install --python 3.12 codegraphcontext`
- Index: `cgc index .` (repo root) · Refresh: `cgc update .` · Stats: `cgc stats`
- Interactive explorer: `cgc visualize --port 8188` then open the printed
  URL (avoid ports 3000/8000/8100 — e2e stack protocol)
- Agent queries: `.mcp.json` registers the MCP server for Claude Code
  sessions (approve on first use); CLI ad-hoc: `cgc query "<cypher>"`
  (function nodes expose `name`, `source`, `lang`, ...)

### CodeCharta 3D code city (hotspot view)

- `make arch-city` → `build/code-city.cc.json` (gitignored; churn metrics
  from git history + code metrics from tree-sitter parsing, merged)
- **View in the browser (recommended): `make arch-city-view`** → open the
  printed URL (`http://localhost:8190/?file=code-city.cc.json`), Ctrl+C to
  stop. This path avoids the Electron app entirely: its postinstall
  downloads a ~100MB binary from GitHub (fails on slow/blocked links) and
  it needs `DISPLAY` set when launched from tmux. If you do want the
  desktop app: `ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
  npx codecharta-visualization` and launch from a terminal with a display
  (its CLI ignores `-p`; pick the file in the app's upload dialog).
- Reading it: map height to `complexity`/`max_complexity_per_function` and
  color to `number_of_commits` — tall + hot buildings are the refactoring
  hotspots (complexity × churn, the "crime scene" signal)
