# CampusQuest top-level verification commands.
# Backend tool versions are committed in backend/pyproject.toml (dev group),
# so `uv run` never drifts; frontend tooling is committed in frontend/package.json.

.PHONY: test-backend test-integration lint-backend verify

test-backend:
	cd backend && uv run pytest tests/unit tests/workers -v

test-integration:
	cd backend && uv run pytest tests/integration -v -m integration

lint-backend:
	cd backend && uv run ruff format --check app tests alembic
	cd backend && uv run ruff check app tests alembic
	cd backend && uv run mypy

verify: lint-backend
	cd backend && uv run pytest -v
	cd frontend && npm run typecheck && npm run lint && npm run build
