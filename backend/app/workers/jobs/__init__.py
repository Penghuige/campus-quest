# backend/app/workers/jobs/__init__.py
"""Job modules: one file per job family, each an orchestration shell.

A job (backend-engineering.md §12) loads IDs and parameters, constructs
dependencies, calls a service, records or logs the result, and retries
according to policy. Domain logic never lives here.
"""
