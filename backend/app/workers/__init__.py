# backend/app/workers/__init__.py
"""Celery worker layer: orchestration shells over application services.

Jobs live in `app.workers.jobs` and run on the Celery app built by
`app.workers.celery_app`. Workers never hold an alternate copy of domain
rules (design spec §3) — see docs/quality/backend-engineering.md §12.
"""
