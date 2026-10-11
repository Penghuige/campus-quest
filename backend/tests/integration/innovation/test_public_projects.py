"""Project aggregation never reads private drafts as public summaries."""

import pytest

from tests.integration.innovation.recruitment_support import clock as clock
from tests.integration.innovation.recruitment_support import command
from tests.integration.innovation.test_achievement_evidence import scanner as scanner
from tests.integration.innovation.test_achievement_evidence import storage as storage
from tests.integration.innovation.test_achievement_review_operations import (
    client as client,
)
from tests.integration.innovation.test_public_recruitments import published

pytestmark = pytest.mark.integration
PROJECTS = "/api/v1/ie/projects"


async def test_project_filters_match_only_effective_public_sources(
    client, db_session, clock
):
    _, project, headers, _, _ = await published(client, db_session, clock)
    for params, count in [
        ({"q": "研究伙伴", "direction": "学习", "stage": "构想"}, 1),
        ({"recruiting_only": True, "beginner_friendly": True}, 1),
        ({"beginner_friendly": False}, 0),
        ({"direction": "PRIVATE_PROJECT_SENTINEL"}, 0),
        ({"stage": "%"}, 0),
    ]:
        response = await client.get(PROJECTS, headers=headers, params=params)
        assert response.status_code == 200, response.text
        assert response.json()["total"] == count
        if count:
            assert response.json()["items"][0]["id"] == str(project.id)
    from uuid import uuid4

    assert (
        await client.get(f"{PROJECTS}/{uuid4()}", headers=headers)
    ).status_code == 404


async def test_project_with_only_recruitment_is_visible_until_closed(
    client, db_session, clock
):
    _, project, headers, base, row = await published(client, db_session, clock)
    response = await client.get(PROJECTS, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    summary = response.json()["items"][0]
    assert summary["id"] == str(project.id)
    assert summary["title"] == "模拟学习项目"
    assert summary["source_kind"] == "RECRUITMENT"
    assert summary["achievement_count"] == 0
    assert summary["recruitment_count"] == 1
    detail = await client.get(PROJECTS + "/" + str(project.id), headers=headers)
    assert detail.status_code == 200
    assert detail.json()["achievements"]["total"] == 0
    assert detail.json()["recruitments"]["total"] == 1
    assert "PRIVATE_PROJECT_SENTINEL" not in detail.text
    assert "demo_partner" not in detail.text
    assert (
        await client.get(
            PROJECTS, headers=headers, params={"q": "PRIVATE_PROJECT_SENTINEL"}
        )
    ).json()["total"] == 0
    assert (
        await client.post(base + "/close", headers=headers, json=command(row))
    ).status_code == 200
    assert (await client.get(PROJECTS, headers=headers)).json()["total"] == 0
    assert (
        await client.get(PROJECTS + "/" + str(project.id), headers=headers)
    ).status_code == 404


async def test_approved_summary_preferred_and_survives_last_recruitment_close(
    client, db_session, clock, storage
):
    from uuid import uuid4

    from tests.integration.innovation.recruitment_support import fields
    from tests.integration.innovation.test_achievement_review_operations import (
        OPS,
        account,
        claim,
        review,
    )

    _, project, achievement, owner_headers, _, _, submitted = await review(
        client, db_session, clock, storage
    )
    _, ops = await account(db_session, clock, operator=True)
    case = submitted["review_case"]
    claimed = await claim(client, case, ops)
    approved = await client.post(
        f"{OPS}/{case['id']}/decision",
        headers=ops,
        json={
            "request_id": str(uuid4()),
            "version": claimed["version"],
            "revision_id": case["revision_id"],
            "decision": "APPROVED",
            "reason": "",
        },
    )
    assert approved.status_code == 200
    original = (
        await client.get(
            f"/api/v1/ie/achievements/{achievement.id}", headers=owner_headers
        )
    ).json()
    base = f"/api/v1/ie/me/project-drafts/{project.id}/recruitments"
    saved = await client.post(
        base,
        headers=owner_headers,
        json={
            "request_id": str(uuid4()),
            "fields": fields() | {"project_title": "免审招募介绍"},
        },
    )
    row = saved.json()
    base += "/" + row["id"]
    row = (
        await client.post(base + "/publish", headers=owner_headers, json=command(row))
    ).json()["current"]
    response = await client.get(PROJECTS + "/" + str(project.id), headers=owner_headers)
    assert response.status_code == 200, response.text
    public = response.json()
    assert public["project"]["title"] == original["project"]["title"]
    assert public["project"]["source_kind"] == "ACHIEVEMENT"
    assert (
        public["project"]["achievement_count"]
        == public["project"]["recruitment_count"]
        == 1
    )
    assert public["achievements"]["items"] == [original]
    assert (
        public["recruitments"]["items"][0]["content"]["project_title"] == "免审招募介绍"
    )
    assert (
        await client.post(base + "/close", headers=owner_headers, json=command(row))
    ).status_code == 200
    retained = await client.get(PROJECTS + "/" + str(project.id), headers=owner_headers)
    assert retained.status_code == 200
    assert retained.json()["achievements"]["items"] == [original]
    assert retained.json()["recruitments"]["total"] == 0
