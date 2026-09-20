# backend/tests/unit/db/test_base.py
"""Unit tests for the declarative Base and its centralized naming convention."""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import ForeignKeyConstraint, Table

from app.db.base import AppMetadata, Base


class _NamingParent(Base):
    __tablename__ = "naming_parent"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    token: Mapped[str] = mapped_column(String(64), index=True)


class _NamingChild(Base):
    __tablename__ = "naming_child"

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("naming_parent.id"))


def test_constraint_and_index_names_follow_central_convention() -> None:
    parent: Table = Base.metadata.tables["naming_parent"]
    child: Table = Base.metadata.tables["naming_child"]

    assert parent.primary_key.name == "pk_naming_parent"

    unique_names = {
        constraint.name
        for constraint in parent.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique_names == {"uq_naming_parent_email"}

    assert {index.name for index in parent.indexes} == {"ix_naming_parent_token"}

    fk_names = {
        constraint.name
        for constraint in child.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert fk_names == {"fk_naming_child_naming_parent_parent_id"}


def test_app_metadata_model_is_registered_on_base_metadata() -> None:
    table = Base.metadata.tables[AppMetadata.__tablename__]

    assert table.primary_key.name == "pk_app_metadata"
    assert {index.name for index in table.indexes} == {"ix_app_metadata_created_at"}
