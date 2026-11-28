"""Managed browser clients keep inspection/attachment separate from provisioning."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from pydantic_ai_backends.browser import (
    BrowserAuthorizationError,
    BrowserConflictError,
    BrowserError,
    BrowserGoneError,
    BrowserUnavailableError,
    SandboxdBrowser,
    WebSocketCDP,
)
from tests.test_browser_session import Transport

_STATE = {
    "environment_id": "session:browser",
    "kind": "browser",
    "state": "ready",
    "created_at": 1.0,
    "last_activity_at": 1.0,
    "expires_at": 901.0,
    "lease_owner": None,
}


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_start_inspect_end_are_explicit_and_credentials_stay_private() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-Sandbox-Token"] == "private-token"
        assert request.url.path == "/prefix/sessions/session/environments/browser"
        return (
            httpx.Response(204) if request.method == "DELETE" else httpx.Response(200, json=_STATE)
        )

    async with client_for(respond) as http:
        client = SandboxdBrowser("https://host/prefix/", "session", "private-token", client=http)
        assert "private-token" not in repr(client)
        assert client.websocket_url == "wss://host/prefix/sessions/session/browser/cdp"
        assert (await client.start(width=640, height=480)).environment_id == "session:browser"
        assert json.loads(requests[0].content) == {
            "width": 640,
            "height": 480,
            "idle_seconds": 900,
            "lifetime_seconds": 3600,
        }
        assert (await client.inspect()).state == "ready"
        await client.end()
    assert [request.method for request in requests] == ["POST", "GET", "DELETE"]
    assert SandboxdBrowser("http://host", "session", "token").websocket_url.startswith("ws://")


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///tmp",
        "http:///missing",
        "http://user:pw@host",
        "http://host?q=x",
        "http://host/#fragment",
    ],
)
def test_rejects_invalid_base_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="base URL"):
        SandboxdBrowser(base_url, "session", "token")


@pytest.mark.parametrize("session_id", ["", "..", "../escape", "x/y", "x" * 65])
def test_rejects_invalid_session_identity(session_id: str) -> None:
    with pytest.raises(ValueError, match="identity"):
        SandboxdBrowser("http://host", session_id, "token")


@pytest.mark.parametrize("token", ["", "header\rbreak", "header\nbreak"])
def test_rejects_missing_or_invalid_credential(token: str) -> None:
    with pytest.raises(ValueError, match="credential"):
        SandboxdBrowser("http://host", "session", token)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_rejects_invalid_request_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        SandboxdBrowser("http://host", "session", "token", timeout=timeout)


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, BrowserAuthorizationError),
        (403, BrowserAuthorizationError),
        (404, BrowserGoneError),
        (410, BrowserGoneError),
        (409, BrowserConflictError),
        (429, BrowserConflictError),
        (503, BrowserUnavailableError),
        (422, BrowserError),
    ],
)
async def test_error_categories_and_no_automatic_mutation_replay(
    status: int, expected: type[Exception]
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"detail": "Provider private data"})

    async with client_for(respond) as http:
        browser = SandboxdBrowser("http://host", "session", "token", client=http)
        with pytest.raises(expected) as failure:
            await browser.start()
        assert "private data" not in str(failure.value)
    assert len(requests) == 1


@pytest.mark.parametrize("status", [404, 410])
async def test_end_of_missing_resource_is_idempotent(status: int) -> None:
    async with client_for(lambda _: httpx.Response(status)) as http:
        await SandboxdBrowser("http://host", "session", "token", client=http).end()


@pytest.mark.parametrize(
    "body", [{}, {**_STATE, "environment_id": "foreign:browser"}, {**_STATE, "kind": "desktop"}]
)
async def test_rejects_invalid_or_foreign_state(body: object) -> None:
    async with client_for(lambda _: httpx.Response(200, json=body)) as http:
        with pytest.raises(BrowserError):
            await SandboxdBrowser("http://host", "session", "token", client=http).inspect()


async def test_transport_failure_is_unavailable_without_replaying() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("failed", request=request)

    async with client_for(fail) as http:
        with pytest.raises(BrowserUnavailableError):
            await SandboxdBrowser("http://host", "session", "token", client=http).start()


async def test_client_owns_and_closes_its_default_http_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = client_for(lambda _: httpx.Response(200, json=_STATE))
    monkeypatch.setattr(httpx, "AsyncClient", lambda: http)
    assert (await SandboxdBrowser("http://host", "session", "token").inspect()).state == "ready"
    assert http.is_closed


@pytest.mark.parametrize("context_id", [None, "own"])
async def test_connect_only_inspects_and_preserves_context_on_detach(
    monkeypatch: pytest.MonkeyPatch, context_id: str | None
) -> None:
    transport = Transport()
    connects: list[tuple[str, object]] = []

    async def connect(_cls: object, endpoint: str, **options: object) -> Transport:
        connects.append((endpoint, options["additional_headers"]))
        return transport

    monkeypatch.setattr(WebSocketCDP, "connect", classmethod(connect))
    methods: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json=_STATE)

    async with client_for(respond) as http:
        client = SandboxdBrowser("http://host", "session", "token", client=http)
        connection = await client.connect(context_id=context_id)
        async with connection:
            assert connection.browser.context_id == "own"
        assert transport.closed
        assert methods == ["GET"]
        assert connects == [
            ("ws://host/sessions/session/browser/cdp", {"X-Sandbox-Token": "token"})
        ]
        await connection.browser.aclose()
        assert all(method != "Target.disposeBrowserContext" for method, _, _ in transport.calls)


async def test_connect_never_heals_missing_context(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = Transport()

    async def connect(_cls: object, _endpoint: str, **_options: object) -> Transport:
        return transport

    monkeypatch.setattr(WebSocketCDP, "connect", classmethod(connect))
    async with client_for(lambda _: httpx.Response(200, json=_STATE)) as http:
        browser = SandboxdBrowser("http://host", "session", "token", client=http)
        with pytest.raises(BrowserError, match="no longer exists"):
            await browser.connect(context_id="missing")
    assert transport.closed
    assert all(method != "Target.createBrowserContext" for method, _, _ in transport.calls)


async def test_connect_refuses_unready_without_opening_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refused(*_args: object, **_kwargs: object) -> Transport:
        raise AssertionError("must not connect")

    monkeypatch.setattr(WebSocketCDP, "connect", refused)
    async with client_for(
        lambda _: httpx.Response(200, json={**_STATE, "state": "expired"})
    ) as http:
        browser = SandboxdBrowser("http://host", "session", "token", client=http)
        with pytest.raises(BrowserUnavailableError, match="not ready"):
            await browser.connect()


async def test_invalid_start_policy_does_not_contact_service() -> None:
    def refused(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not contact service")

    async with client_for(refused) as http:
        browser = SandboxdBrowser("http://host", "session", "token", client=http)
        with pytest.raises(ValueError):
            await browser.start(idle_seconds=86401)


async def test_failed_attach_cleanup_preserves_original_refusal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenClose(Transport):
        async def aclose(self) -> None:
            raise OSError("private cleanup payload")

    transport = BrokenClose()

    async def connect(_cls: object, _endpoint: str, **_options: object) -> Transport:
        return transport

    monkeypatch.setattr(WebSocketCDP, "connect", classmethod(connect))
    async with client_for(lambda _: httpx.Response(200, json=_STATE)) as http:
        browser = SandboxdBrowser("http://host", "session", "token", client=http)
        with pytest.raises(BrowserError, match="no longer exists"):
            await browser.connect(context_id="missing")
    assert "managed_browser_detach_failed: OSError" in caplog.text
    assert "private cleanup payload" not in caplog.text


async def test_client_never_follows_redirect_with_credential() -> None:
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://foreign.example/steal"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(redirect), follow_redirects=True
    ) as http:
        browser = SandboxdBrowser("http://host", "session", "private-token", client=http)
        with pytest.raises(BrowserError, match="HTTP 302"):
            await browser.inspect()
    assert len(requests) == 1


async def test_temporary_provider_refusal_is_not_authoritative_resource_loss() -> None:
    async with client_for(lambda _: httpx.Response(503)) as http:
        with pytest.raises(BrowserUnavailableError) as raised:
            await SandboxdBrowser("http://host", "session", "token", client=http).inspect()
    assert not isinstance(raised.value, BrowserGoneError)
