# backend/app/db/base.py
"""Declarative Base with centralized constraint and index naming (§8).

All ORM models derive from `Base`, so constraint and index names stay
deterministic across Alembic autogenerate runs. Model modules introduced by
later features must be imported at the bottom of this module so
`Base.metadata` aggregates every table for Alembic.
"""

from datetime import datetime

from sqlalchemy import DateTime, MetaData, String, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(referred_table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Shared declarative base carrying the project naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class AppMetadata(Base):
    """Foundation key/value table created by migration 0001.

    Harmless on its own: it proves the migration pipeline end to end and
    gives later modules a place to record release-level facts.
    """

    __tablename__ = "app_metadata"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), index=True
    )


# Aggregate model modules so `Base.metadata` covers every table for Alembic
# autogenerate (see module docstring). The import must stay below the Base
# class definition: model modules import Base from this module.
from app.modules.identity import models as identity_models  # noqa: E402, F401
from app.modules.points import models as point_models  # noqa: E402, F401
from app.modules.submissions import models as submission_models  # noqa: E402, F401
from app.modules.tasks import models as task_models  # noqa: E402, F401
