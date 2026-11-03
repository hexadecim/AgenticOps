"""Protocol-level stand-ins for the real providers, faithful enough to exercise each adapter's read
position, paging and parsing. The live GreenMail run in the contract suite checks the IMAP adapter
against a real server.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
import respx

BASE_TIME = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)


class StubImapServer:
    def __init__(self, password: str = "pw") -> None:
        self.password = password
        self.uidvalidity = 7001
        self.messages: list[tuple[int, bytes, datetime]] = []
        self._next = 1

    def deliver(self, raw: bytes) -> None:
        self.messages.append((self._next, raw, BASE_TIME + timedelta(seconds=self._next)))
        self._next += 1

    def client(self, host: str, port: int, use_ssl: bool, timeout: float) -> StubImapClient:
        return StubImapClient(self)


class StubImapClient:
    """Implements the subset of imaplib.IMAP4 the adapter uses, including the `N:*` quirk."""

    def __init__(self, server: StubImapServer) -> None:
        self._server = server

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        import imaplib

        if password != self._server.password:
            raise imaplib.IMAP4.error("LOGIN failed")
        return "OK", [b"LOGIN completed"]

    def status(self, folder: str, items: str) -> tuple[str, list[bytes]]:
        if folder.strip('"') != "INBOX":
            return "NO", [b"no such folder"]
        return "OK", [
            f"{folder} (UIDNEXT {self._server._next} UIDVALIDITY {self._server.uidvalidity})".encode()
        ]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        assert readonly, "the adapter must never open the folder read-write"
        return "OK", [str(len(self._server.messages)).encode()]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "SEARCH":
            uids = [u for u, _, _ in self._server.messages]
            if args[1] == "SINCE":
                since = datetime.strptime(args[2], "%d-%b-%Y").replace(tzinfo=UTC)
                found = [u for u, _, at in self._server.messages if at >= since]
            else:
                start = int(re.match(r"UID (\d+):\*", args[1]).group(1))  # type: ignore[union-attr]
                found = [u for u in uids if u >= start] or uids[-1:]  # RFC 3501: `*` is the highest UID
            return "OK", [" ".join(map(str, found)).encode()]
        if command == "FETCH":
            for u, raw, at in self._server.messages:
                if u == int(args[0]):
                    date = at.strftime("%d-%b-%Y %H:%M:%S +0000")
                    meta = f'1 (UID {u} INTERNALDATE "{date}" BODY[] {{{len(raw)}}}'.encode()
                    return "OK", [(meta, raw), b")"]
            return "OK", [None]
        raise AssertionError(f"unexpected UID {command}")

    def logout(self) -> None:
        pass


class StubSmtp:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def client(self, host: str, port: int, timeout: float) -> StubSmtp:
        return self

    def has_extn(self, name: str) -> bool:
        return name == "auth"

    def login(self, user: str, password: str) -> None:
        pass

    def sendmail(self, sender: str, recipients: list[str], raw: bytes) -> dict[str, Any]:
        self.sent.append(raw)
        return {}

    def quit(self) -> None:
        pass


TOKEN = {"access_token": "access-1", "expires_in": 3600}


class GmailStub:
    """Gmail REST v1: list (newest first, `after:` inclusive by second), get raw, send."""

    API = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, router: respx.Router) -> None:
        self.messages: list[dict[str, Any]] = []
        self.sent: list[bytes] = []
        router.post("https://oauth2.googleapis.com/token").mock(return_value=httpx.Response(200, json=TOKEN))
        router.get(f"{self.API}/messages").mock(side_effect=self._list)
        router.get(url__regex=rf"{re.escape(self.API)}/messages/[^/]+$").mock(side_effect=self._get)
        router.post(f"{self.API}/messages/send").mock(side_effect=self._send)

    def deliver(self, raw: bytes) -> None:
        n = len(self.messages) + 1
        at = int((BASE_TIME + timedelta(seconds=n // 2)).timestamp() * 1000)  # pairs share a second
        self.messages.append({"id": f"g{n:04d}", "threadId": f"t{n}", "internalDate": str(at), "raw": raw})

    def _list(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer access-1"
        assert request.url.params["labelIds"] == "INBOX"
        after = int(request.url.params["q"].removeprefix("after:"))
        found = [m for m in self.messages if int(m["internalDate"]) // 1000 >= after]
        return httpx.Response(200, json={"messages": [{"id": m["id"]} for m in reversed(found)]})

    def _get(self, request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "raw"
        ident = request.url.path.rsplit("/", 1)[1]
        m = next((m for m in self.messages if m["id"] == ident), None)
        if m is None:
            return httpx.Response(404, json={})
        raw = base64.urlsafe_b64encode(m["raw"]).decode().rstrip("=")
        return httpx.Response(
            200,
            json={"id": m["id"], "threadId": m["threadId"], "internalDate": m["internalDate"], "raw": raw},
        )

    def _send(self, request: httpx.Request) -> httpx.Response:
        raw = json.loads(request.content)["raw"]
        self.sent.append(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return httpx.Response(200, json={"id": "sent-1"})


class GraphStub:
    """Graph v1.0: list by receivedDateTime (ge, asc, $top), `/$value` MIME, sendMail with base64 MIME."""

    USER = "https://graph.microsoft.com/v1.0/users/support%40example.com"

    def __init__(self, router: respx.Router) -> None:
        self.messages: list[dict[str, Any]] = []
        self.sent: list[bytes] = []
        router.post(url__regex=r"https://login\.microsoftonline\.com/.+/token").mock(
            return_value=httpx.Response(200, json=TOKEN)
        )
        router.get(f"{self.USER}/mailFolders/inbox/messages").mock(side_effect=self._list)
        router.get(url__regex=rf"{re.escape(self.USER)}/messages/[^/]+/\$value").mock(side_effect=self._value)
        router.post(f"{self.USER}/sendMail").mock(side_effect=self._send)

    def deliver(self, raw: bytes) -> None:
        n = len(self.messages) + 1
        at = (BASE_TIME + timedelta(seconds=n // 2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.messages.append(
            {"id": f"AAMk{n:04d}", "conversationId": f"c{n}", "receivedDateTime": at, "raw": raw}
        )

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        since = params["$filter"].removeprefix("receivedDateTime ge ")
        assert params["$orderby"] == "receivedDateTime asc"
        found = [m for m in self.messages if m["receivedDateTime"] >= since]
        top = int(params["$top"])
        body: dict[str, Any] = {
            "value": [{k: m[k] for k in ("id", "conversationId", "receivedDateTime")} for m in found[:top]]
        }
        if len(found) > top:
            body["@odata.nextLink"] = "https://graph.microsoft.com/next"
        return httpx.Response(200, json=body)

    def _value(self, request: httpx.Request) -> httpx.Response:
        ident = unquote(request.url.path.split("/messages/")[1].removesuffix("/$value"))
        m = next((m for m in self.messages if m["id"] == ident), None)
        return httpx.Response(200, content=m["raw"]) if m else httpx.Response(404)

    def _send(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "text/plain"
        self.sent.append(base64.b64decode(request.content))
        return httpx.Response(202)
