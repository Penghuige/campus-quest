# backend/app/integrations/__init__.py
"""External-system adapter ports: object storage, SMS, email.

Domain modules consume these Protocols only; provider SDKs stay in real
adapters (added by later plans), tests use the fakes under
`backend/tests/fakes/` (docs/architecture/interfaces.md, Adapter Ports).
"""
