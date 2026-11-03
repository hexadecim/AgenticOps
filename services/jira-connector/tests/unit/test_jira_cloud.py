from __future__ import annotations

import json

import httpx
import pytest
import respx
from jira_connector.adapters.outbound.jira_cloud import JiraCloudTracker
from jira_connector.domain.errors import TrackerRejectedError, TrackerUnavailableError
from jira_connector.domain.model import TicketFields
from opspilot_common.resilience import ResilienceRegistry, ResilientHttpClient

from ..conftest import SETTINGS

BASE = SETTINGS.base_url


def tracker() -> JiraCloudTracker:
    http = ResilientHttpClient.from_registry(ResilienceRegistry(), "jira", base_url=BASE, auth=("bot", "t"))
    return JiraCloudTracker(SETTINGS, http)


@respx.mock
async def test_create_sends_adf_and_labels() -> None:
    route = respx.post(f"{BASE}/rest/api/3/issue").mock(
        return_value=httpx.Response(201, json={"key": "KAN-15"})
    )
    key = await tracker().create(
        TicketFields("[SITE-A014] Upload stuck", "Line 1\nLine 2"), ["opspilot", "opspilot-a-1"]
    )
    assert key == "KAN-15"
    fields = json.loads(route.calls.last.request.content)["fields"]
    assert fields["project"] == {"key": "KAN"} and fields["issuetype"] == {"name": "Incident"}
    assert fields["description"]["type"] == "doc" and fields["labels"] == ["opspilot", "opspilot-a-1"]
    assert route.calls.last.request.headers["authorization"].startswith("Basic ")


@respx.mock
async def test_search_and_label_lookup() -> None:
    route = respx.post(f"{BASE}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(
            200,
            json={
                "issues": [
                    {
                        "key": "KAN-3",
                        "fields": {
                            "summary": "Upload down",
                            "status": {"name": "In Progress"},
                            "created": "2026-10-08",
                        },
                    }
                ]
            },
        )
    )
    recent = await tracker().recent(24)
    assert [(i.key, i.status) for i in recent] == [("KAN-3", "In Progress")]
    jql = json.loads(route.calls.last.request.content)["jql"]
    assert 'project = "KAN"' in jql and "created >= -24h" in jql and "statusCategory != Done" in jql
    assert await tracker().find_by_label("opspilot-a-1") == "KAN-3"


@respx.mock
async def test_refusals_and_outages_are_told_apart() -> None:
    respx.post(f"{BASE}/rest/api/3/issue").mock(
        return_value=httpx.Response(
            400, json={"errorMessages": [], "errors": {"issuetype": "Specify a valid issue type"}}
        )
    )
    with pytest.raises(TrackerRejectedError, match="issuetype: Specify a valid issue type"):
        await tracker().create(TicketFields("s", "d"), [])
    respx.post(f"{BASE}/rest/api/3/issue/KAN-3/comment").mock(return_value=httpx.Response(503))
    with pytest.raises(TrackerUnavailableError):
        await tracker().comment("KAN-3", "hello")
    respx.post(f"{BASE}/rest/api/3/search/jql").mock(return_value=httpx.Response(401))
    with pytest.raises(TrackerRejectedError, match="credentials"):
        await tracker().recent(24)
