"""Private innovation preparation, separate from Task and publication.

This package's ``__init__`` stays side-effect-free on purpose:
``app/db/base.py`` imports ``innovation.talent_models`` to aggregate
tables, and a router-importing ``__init__`` there would cycle back
into the audit module mid-initialization. The mounted aggregate lives
in ``aggregated``.
"""
