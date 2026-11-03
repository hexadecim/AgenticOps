from __future__ import annotations

import pytest
from settings_service.application import GetSection, RunConnectionTest, SaveSection
from settings_service.domain.catalogue import JIRA, MAILBOX
from settings_service.domain.errors import InvalidSettingsError, VersionConflictError
from settings_service.ports import Check, ConnectionReport

from ..conftest import IMAP_VALUES, FixedClock, World


def test_imap_fields_required_only_for_imap() -> None:
    with pytest.raises(InvalidSettingsError) as err:
        MAILBOX.normalise({"provider": "imap"}, {})
    assert {"imap_host", "username", "password"} <= err.value.errors.keys()
    assert "graph_tenant_id" not in err.value.errors
    with pytest.raises(InvalidSettingsError) as err:
        MAILBOX.normalise({"provider": "graph"}, {})
    assert "imap_host" not in err.value.errors and "graph_client_secret" in err.value.errors


def test_coercion_messages_and_defaults() -> None:
    with pytest.raises(InvalidSettingsError) as err:
        MAILBOX.normalise({**IMAP_VALUES, "imap_port": "abc", "poll_interval_seconds": 5}, {"password": "p"})
    assert err.value.errors == {
        "imap_port": "Enter a whole number.",
        "poll_interval_seconds": "Use 30 or more.",
    }
    values, _ = MAILBOX.normalise({**IMAP_VALUES, "unknown": "x"}, {"password": "p"})
    assert values["poll_interval_seconds"] == 120 and values["imap_ssl"] is True and "unknown" not in values
    with pytest.raises(InvalidSettingsError) as err:
        JIRA.normalise(
            {"base_url": "jira.example.com", "email": "a@b.c", "project_key": "KAN"}, {"api_token": "t"}
        )
    assert "base_url" in err.value.errors


async def test_save_versions_masks_secrets_and_records_field_names_only(world: World) -> None:
    save, get = SaveSection(world.uow, FixedClock()), GetSection(world.uow)
    view = await save("admin-1", "mailbox", IMAP_VALUES, {"password": "s3cret-app-pw"}, expected_version=0)
    assert view.version == 1 and view.secrets_set == {
        "gmail_client_secret": False,
        "gmail_refresh_token": False,
        "graph_client_secret": False,
        "password": True,
    }
    payload, actor = world.events[-1]
    assert actor == "admin-1" and "password" in payload.changed_fields
    assert "s3cret-app-pw" not in payload.model_dump_json()

    # Omitted secret is kept; unchanged save is a no-op (no new version, no event).
    view = await save("admin-1", "mailbox", IMAP_VALUES, {}, expected_version=1)
    assert view.version == 1 and len(world.events) == 1
    assert world.store.rows["mailbox"].secrets == {"password": "s3cret-app-pw"}

    view = await save(
        "admin-1", "mailbox", {**IMAP_VALUES, "poll_interval_seconds": 60}, {}, expected_version=1
    )
    assert view.version == 2 and world.events[-1][0].changed_fields == ["poll_interval_seconds"]
    assert (await get("mailbox")).values["poll_interval_seconds"] == 60


async def test_stale_version_is_refused(world: World) -> None:
    save = SaveSection(world.uow, FixedClock())
    await save("a", "mailbox", IMAP_VALUES, {"password": "p"}, expected_version=0)
    with pytest.raises(VersionConflictError):
        await save("b", "mailbox", {**IMAP_VALUES, "folder": "Support"}, {}, expected_version=0)


async def test_switching_provider_drops_old_fields_and_secrets(world: World) -> None:
    save = SaveSection(world.uow, FixedClock())
    await save("a", "mailbox", IMAP_VALUES, {"password": "p"}, expected_version=0)
    graph = {"provider": "graph", "graph_tenant_id": "t", "graph_client_id": "c", "graph_mailbox": "s@x.eu"}
    await save("a", "mailbox", graph, {"graph_client_secret": "g"}, expected_version=1)
    doc = world.store.rows["mailbox"]
    assert "imap_host" not in doc.values and doc.secrets == {"graph_client_secret": "g"}


async def test_clearing_a_required_secret_is_refused(world: World) -> None:
    save = SaveSection(world.uow, FixedClock())
    await save("a", "mailbox", IMAP_VALUES, {"password": "p"}, expected_version=0)
    with pytest.raises(InvalidSettingsError) as err:
        await save("a", "mailbox", IMAP_VALUES, {"password": ""}, expected_version=1)
    assert "password" in err.value.errors


class RecordingTester:
    def __init__(self) -> None:
        self.seen: tuple[dict, dict] | None = None  # type: ignore[type-arg]

    async def test(self, values: dict, secrets: dict) -> ConnectionReport:  # type: ignore[type-arg]
        self.seen = (values, secrets)
        return ConnectionReport.from_checks([Check("x", True, "fine")], "All good")


async def test_test_uses_unsaved_values_with_stored_secrets(world: World) -> None:
    await SaveSection(world.uow, FixedClock())(
        "a", "mailbox", IMAP_VALUES, {"password": "stored"}, expected_version=0
    )
    imap, graph = RecordingTester(), RecordingTester()
    tester = RunConnectionTest(world.uow, {"mailbox:imap": imap, "mailbox:graph": graph})
    report = await tester("mailbox", {**IMAP_VALUES, "imap_host": "new.example.com"}, {})
    assert report.ok and imap.seen and imap.seen[0]["imap_host"] == "new.example.com"
    assert imap.seen[1] == {"password": "stored"} and graph.seen is None


async def test_test_reports_missing_fields_instead_of_calling_out(world: World) -> None:
    tester = RecordingTester()
    report = await RunConnectionTest(world.uow, {"jira": tester})(
        "jira", {"base_url": "https://x.atlassian.net"}, {}
    )
    assert not report.ok and tester.seen is None
    assert {c.name for c in report.checks} >= {"Account email", "API token", "Project key"}
