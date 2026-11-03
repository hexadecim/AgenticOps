from __future__ import annotations

import imaplib
import socket

import httpx
import pytest
import respx
from opspilot_common.resilience import ResilienceRegistry, ResilienceSettings, ResilientHttpClient
from settings_service.adapters.outbound.testers.gmail import GmailTester
from settings_service.adapters.outbound.testers.graph import GraphTester
from settings_service.adapters.outbound.testers.imap import ImapTester
from settings_service.adapters.outbound.testers.jira import JiraTester
from settings_service.adapters.outbound.testers.ollama import OllamaTester

FAST = ResilienceSettings.from_mapping({"defaults": {"timeout": 2, "retry": {"attempts": 1}}})


def client(name: str) -> ResilientHttpClient:
    return ResilientHttpClient.from_registry(ResilienceRegistry(FAST), name)


LLM = {"ollama_url": "http://ollama:11434", "model": "deepseek-r1:8b", "embedding_model": "nomic-embed-text"}


@respx.mock
async def test_ollama_ready_and_missing_model() -> None:
    respx.get("http://ollama:11434/api/version").mock(
        return_value=httpx.Response(200, json={"version": "0.34.4"})
    )
    tags = respx.get("http://ollama:11434/api/tags").mock(
        return_value=httpx.Response(
            200, json={"models": [{"name": "deepseek-r1:8b"}, {"name": "nomic-embed-text:latest"}]}
        )
    )
    report = await OllamaTester(client("probe-ollama")).test(LLM, {})
    assert report.ok, report
    tags.mock(return_value=httpx.Response(200, json={"models": [{"name": "llama3:8b"}]}))
    report = await OllamaTester(client("probe-ollama")).test(LLM, {})
    assert not report.ok and "ollama pull deepseek-r1:8b" in report.summary


@respx.mock
async def test_ollama_unreachable() -> None:
    respx.get("http://ollama:11434/api/version").mock(side_effect=httpx.ConnectError("refused"))
    report = await OllamaTester(client("probe-ollama")).test(LLM, {})
    assert not report.ok and report.summary.startswith("Cannot reach Ollama")


JIRA = {
    "base_url": "https://acme.atlassian.net",
    "email": "bot@acme.eu",
    "project_key": "kan",
    "issue_type": "Incident",
}


@respx.mock
async def test_jira_full_success() -> None:
    respx.get(f"{JIRA['base_url']}/rest/api/3/myself").mock(
        return_value=httpx.Response(200, json={"displayName": "Ops Bot"})
    )
    respx.get(f"{JIRA['base_url']}/rest/api/3/project/KAN").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Support",
                "issueTypes": [{"name": "Task"}, {"name": "Incident"}, {"name": "Sub-task", "subtask": True}],
            },
        )
    )
    respx.get(f"{JIRA['base_url']}/rest/api/3/mypermissions").mock(
        return_value=httpx.Response(200, json={"permissions": {"CREATE_ISSUES": {"havePermission": True}}})
    )
    report = await JiraTester(client("probe-jira")).test(JIRA, {"api_token": "t"})
    assert report.ok and report.summary == "Ready to create Incident issues in KAN."
    auth = respx.calls[0].request.headers["Authorization"]
    assert auth.startswith("Basic ")


@respx.mock
async def test_jira_bad_token_and_missing_issue_type() -> None:
    me = respx.get(f"{JIRA['base_url']}/rest/api/3/myself").mock(return_value=httpx.Response(401, json={}))
    report = await JiraTester(client("probe-jira")).test(JIRA, {"api_token": "bad"})
    assert report.summary == "Jira rejected the account email or API token."
    me.mock(return_value=httpx.Response(200, json={"displayName": "Bot"}))
    respx.get(f"{JIRA['base_url']}/rest/api/3/project/KAN").mock(
        return_value=httpx.Response(
            200, json={"name": "Support", "issueTypes": [{"name": "Task"}, {"name": "Bug"}]}
        )
    )
    respx.get(f"{JIRA['base_url']}/rest/api/3/mypermissions").mock(return_value=httpx.Response(200, json={}))
    report = await JiraTester(client("probe-jira")).test(JIRA, {"api_token": "t"})
    assert "Available: Task, Bug" in report.summary
    assert [c.ok for c in report.checks] == [True, True, False, False]


@respx.mock
async def test_gmail_wrong_mailbox() -> None:
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a"})
    )
    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/profile").mock(
        return_value=httpx.Response(200, json={"emailAddress": "other@acme.eu"})
    )
    respx.get("https://gmail.googleapis.com/gmail/v1/users/me/labels").mock(
        return_value=httpx.Response(200, json={"labels": [{"id": "INBOX", "name": "INBOX"}]})
    )
    values = {"gmail_user": "support@acme.eu", "gmail_client_id": "c", "folder": "INBOX"}
    report = await GmailTester(client("probe-google")).test(
        values, {"gmail_client_secret": "s", "gmail_refresh_token": "r"}
    )
    assert not report.ok and "belongs to other@acme.eu" in report.summary


@respx.mock
async def test_graph_permission_hint() -> None:
    respx.post("https://login.microsoftonline.com/tid/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a"})
    )
    respx.get("https://graph.microsoft.com/v1.0/users/support%40acme.eu/mailFolders/inbox").mock(
        return_value=httpx.Response(403, json={})
    )
    values = {
        "graph_tenant_id": "tid",
        "graph_client_id": "c",
        "graph_mailbox": "support@acme.eu",
        "folder": "INBOX",
    }
    report = await GraphTester(client("probe-microsoft")).test(values, {"graph_client_secret": "s"})
    assert not report.ok and "Mail.ReadWrite" in report.summary


class FakeImap:
    def __init__(self, *, password: str = "right", folders: dict[str, int] | None = None) -> None:
        self._password, self._folders = password, folders or {"INBOX": 42}

    def login(self, user: str, password: str) -> None:
        if password != self._password:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        return ("OK", [str(self._folders[folder]).encode()]) if folder in self._folders else ("NO", [b""])

    def logout(self) -> None:
        pass


IMAP = {
    "imap_host": "imap.acme.eu",
    "imap_port": 993,
    "imap_ssl": True,
    "username": "s@acme.eu",
    "folder": "INBOX",
}


@pytest.mark.parametrize(
    ("password", "folder", "ok", "summary"),
    [
        ("right", "INBOX", True, "OpsPilot can read INBOX at imap.acme.eu."),
        ("wrong", "INBOX", False, "The mail server rejected the user name or password."),
        ("right", "Support", False, "Signed in, but the folder 'Support' does not exist."),
    ],
)
async def test_imap_outcomes(password: str, folder: str, ok: bool, summary: str) -> None:
    tester = ImapTester(ResilienceRegistry(FAST).get("probe-mail"), imap_factory=lambda *a: FakeImap())  # type: ignore[arg-type,return-value]
    report = await tester.test({**IMAP, "folder": folder}, {"password": password})
    assert report.ok is ok and report.summary.startswith(summary)


async def test_imap_unreachable_counts_against_breaker() -> None:
    def refuse(*_: object) -> imaplib.IMAP4:
        raise socket.gaierror("Name or service not known")

    registry = ResilienceRegistry(FAST)
    tester = ImapTester(registry.get("probe-mail"), imap_factory=refuse)
    report = await tester.test(IMAP, {"password": "x"})
    assert not report.ok and report.summary.startswith("Cannot reach the IMAP server imap.acme.eu:993")


@respx.mock
async def test_ops_tester_checks_each_probe_target() -> None:
    from opspilot_common.resilience import ResilienceRegistry, ResilientHttpClient
    from settings_service.adapters.outbound.testers.ops import OpsTester

    respx.get("https://portal.example.com/health").mock(return_value=httpx.Response(200))
    respx.get("https://login.example.com/health").mock(return_value=httpx.Response(503))
    tester = OpsTester(ResilientHttpClient.from_registry(ResilienceRegistry(), "probe-ops"))
    report = await tester.test({"probe_targets": ""}, {})
    assert report.ok and [c.name for c in report.checks] == ["Executor", "Probes"]
    report = await tester.test(
        {"probe_targets": "Portal=https://portal.example.com/health, Login=https://login.example.com/health"},
        {},
    )
    assert not report.ok
    checks = {c.name: c for c in report.checks}
    assert checks["Portal"].ok and not checks["Login"].ok
    assert not (await tester.test({"probe_targets": "Portal=ftp://x"}, {})).ok
