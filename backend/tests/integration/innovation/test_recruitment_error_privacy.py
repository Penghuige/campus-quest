"""Real PostgreSQL errors must not disclose contact values or draft bodies in logs."""

import logging
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.integration.innovation.recruitment_support import client as client
from tests.integration.innovation.recruitment_support import clock as clock
from tests.integration.innovation.recruitment_support import fields, world

pytestmark = pytest.mark.integration


async def test_database_error_logs_keep_diagnostics_without_private_payload(
    db_session, client, clock, caplog
):
    _, _, headers, base = await world(db_session, clock)
    payload = fields() | {
        "introduction": "PRIVATE_R3_BODY_SENTINEL",
        "contacts": [{"channel": "EMAIL", "value": "private_r3@example.test"}],
    }
    # Trigger is scoped to the rollback-only test transaction. The database's
    # own error detail deliberately includes the row, as real constraint errors do.
    await db_session.execute(
        text("""
        CREATE FUNCTION pg_temp.r3_recruitment_fail() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'controlled persistence failure' USING
                DETAIL = NEW.fields::text;
        END $$
    """)
    )
    await db_session.execute(
        text("""
        CREATE TRIGGER r3_private_error BEFORE INSERT ON ie_recruitment_drafts
        FOR EACH ROW EXECUTE FUNCTION pg_temp.r3_recruitment_fail()
    """)
    )
    with caplog.at_level(logging.ERROR, logger="app.core.errors"):
        response = await client.post(
            base,
            headers=headers | {"X-Request-ID": "r3-private-db-error"},
            json={"request_id": str(uuid4()), "fields": payload},
        )
    assert response.status_code == 500, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["error"]["request_id"] == "r3-private-db-error"
    for private in (payload["introduction"], payload["contacts"][0]["value"]):
        assert private not in response.text
        assert private not in caplog.text
    assert "r3-private-db-error" in caplog.text
    assert "DBAPIError" in caplog.text
    assert "P0001" in caplog.text
