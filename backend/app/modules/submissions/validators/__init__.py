# backend/app/modules/submissions/validators/__init__.py
"""Submission format validators (spec §12; plan 04 tasks 4-6).

Each validator turns an untrusted uploaded file into a structured
``ValidationReport`` under strict resource bounds
(docs/quality/backend-engineering.md §14); shared limits, codes, the
report builder, and the per-cell type checks live in ``common``.
"""
