"""Tests for the remote sandbox protocol, client and sandboxd service.

The service is driven in-process through Starlette's `TestClient`, with a fake
sandbox builder injected, so nothing here needs a Docker daemon. The same
`TestClient` is handed to `RemoteSandbox` as its HTTP client, which makes the
client/server pair genuinely end-to-end rather than mocked against each other.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
import types
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pydantic_ai_backends import StateBackend
from pydantic_ai_backends.remote import RemoteSandbox, wire
from pydantic_ai_backends.remote.server import (
    SandboxdConfig,
    _session_volumes,
    create_app,
)
from pydantic_ai_backends.types import (
    EditResult,
    ExecuteResponse,
    SandboxUsage,
    WriteResult,
)

SERVICE_TOKEN = "service-secret"


class FakeSandbox:
    """In-memory stand-in for a Docker sandbox.

    Backed by `StateBackend` for the file operations so the tests exercise real
    read/write/edit/glob/grep behaviour rather than canned responses. Raw bytes
    are additionally kept verbatim in `_blobs`, because `StateBackend` stores
    text lines and is not byte-exact — without that, a binary round trip would
    fail on the fake rather than on the protocol under test.
    """

    def __init__(self, session_id: str, runtime: Any) -> None:
        self._id = session_id
        self.runtime_entry = runtime
        self.image = runtime.image_label()
        self._store = StateBackend()
        self._blobs: dict[str, bytes] = {}
        self._last_activity = 1_000.0
        self._idle_timeout = 60
        self.alive = False
        self.started = 0
        self.stopped = 0
        self.removed = False
        # False models the base sandbox surface, whose `stop()` takes no
        # arguments — `_remove_sandbox` has to fall back for those.
        self.accepts_remove = True
        self.commands: list[tuple[str, int | None]] = []
        self.usage: SandboxUsage | None = None
        self.usage_calls = 0
        self.sample_delay = 0.0
        self.start_error: Exception | None = None

    # lifecycle -----------------------------------------------------------
    @property
    def session_id(self) -> str:
        return self._id

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started += 1
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def stop(self, remove: bool = False) -> None:
        if remove and not self.accepts_remove:
            raise TypeError("stop() got an unexpected keyword argument 'remove'")
        self.stopped += 1
        self.alive = False
        if remove:
            self.removed = True

    def resource_usage(self) -> SandboxUsage | None:
        self.usage_calls += 1
        if self.sample_delay:
            # Docker's stats endpoint waits for a second sample before it can
            # report a CPU rate, which is why a real one takes over a second.
            time.sleep(self.sample_delay)
        return self.usage

    # operations ----------------------------------------------------------
    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        self.commands.append((command, timeout))
        return ExecuteResponse(output=f"ran {command}", exit_code=0, truncated=False)

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        return self._store.read(path, offset, limit)

    def read_bytes(self, path: str) -> bytes:
        if path in self._blobs:
            return self._blobs[path]
        return self._store.read_bytes(path)

    def write(self, path: str, content: str | bytes) -> WriteResult:
        raw = content if isinstance(content, bytes) else content.encode("utf-8")
        self._blobs[path] = raw
        return self._store.write(path, content)

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return self._store.edit(path, old_string, new_string, replace_all)

    def exists(self, path: str) -> bool:
        return self._store.exists(path)

    def ls_info(self, path: str) -> list[Any]:
        return self._store.ls_info(path)

    def glob_info(self, pattern: str, path: str = "/") -> list[Any]:
        return self._store.glob_info(pattern, path)

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ) -> Any:
        return self._store.grep_raw(pattern, path, glob, ignore_hidden)


class Harness:
    """A running service plus the sandboxes it handed out."""

    def __init__(self, **config_kwargs: Any) -> None:
        self.built: dict[str, FakeSandbox] = {}
        self.next_start_error: Exception | None = None
        config = SandboxdConfig(
            token=SERVICE_TOKEN,
            runtimes={"python": "python:3.12-slim", "node": "node:20-slim"},
            default_runtime="python",
            **config_kwargs,
        )
        self.config = config
        self.app = create_app(config, sandbox_builder=self._build)

    def _build(self, session_id: str, runtime: Any) -> FakeSandbox:
        # The real builder creates the session's host workspace; this stands in
        # for it, so tests about purging and sweeping see the same directories.
        _session_volumes(self.config, session_id)
        sandbox = FakeSandbox(session_id, runtime)
        sandbox.start_error = self.next_start_error
        self.built[session_id] = sandbox
        return sandbox

    def client(self) -> TestClient:
        return TestClient(self.app)


@pytest.fixture
def harness() -> Harness:
    return Harness()


@pytest.fixture
def client(harness: Harness):
    with harness.client() as running:
        yield running


def _service_headers() -> dict[str, str]:
    return {wire.TOKEN_HEADER: SERVICE_TOKEN}


def _open_session(client: TestClient, **body: Any) -> tuple[str, str]:
    """Open a session, returning `(session_id, session_token)`."""
    response = client.post("/sessions", json=body, headers=_service_headers())
    assert response.status_code == 200, response.text
    created = wire.SessionCreated.model_validate(response.json())
    return created.session.session_id, created.token


class TestSandboxdConfig:
    """Validation of the service policy object."""

    def test_token_is_required(self):
        with pytest.raises(ValueError, match="token must not be empty"):
            SandboxdConfig(token="")

    def test_at_least_one_runtime_is_required(self):
        with pytest.raises(ValueError, match="at least one image"):
            SandboxdConfig(token="t", runtimes={})

    def test_default_runtime_must_be_allowed(self):
        with pytest.raises(ValueError, match="not in runtimes"):
            SandboxdConfig(token="t", runtimes={"a": "img"}, default_runtime="b")

    def test_resolve_runtime_uses_the_default_for_none(self):
        config = SandboxdConfig(token="t", runtimes={"a": "img-a"}, default_runtime="a")

        alias, runtime = config.resolve_runtime(None)

        assert alias == "a"
        assert runtime.image == "img-a"

    def test_resolve_runtime_rejects_an_unknown_alias(self):
        config = SandboxdConfig(token="t", runtimes={"a": "img-a"}, default_runtime="a")
        with pytest.raises(KeyError):
            config.resolve_runtime("nope")


class TestIndex:
    """The root route — opening the base URL should not 404."""

    def test_root_describes_the_service(self, client: TestClient):
        response = client.get("/")

        assert response.status_code == 200
        body = wire.ServiceIndex.model_validate(response.json())
        assert body.service == "sandboxd"
        assert body.health.status == "ok"
        assert body.docs_url == "/docs"

    def test_root_lists_the_real_routes(self, client: TestClient):
        """Derived from the app, so it cannot drift from what is mounted."""
        body = wire.ServiceIndex.model_validate(client.get("/").json())

        assert "/healthz" in body.endpoints
        assert "/sessions" in body.endpoints
        assert "/sessions/{session_id}/exec" in body.endpoints
        assert "/" not in body.endpoints

    def test_root_needs_no_token(self, client: TestClient):
        assert client.get("/").status_code == 200

    def test_ui_is_absent_unless_enabled(self, client: TestClient):
        """The dashboard is opt-in; the API is unaffected either way."""
        body = wire.ServiceIndex.model_validate(client.get("/").json())

        assert body.ui_url is None
        assert client.get("/ui").status_code == 404
        assert "/ui" not in body.endpoints


class TestDashboard:
    """The optional bundled UI."""

    def test_ui_is_served_when_enabled(self):
        harness = Harness(ui_enabled=True)
        with harness.client() as client:
            response = client.get("/ui")

            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/html")
            assert "sandboxd" in response.text
            # Self-contained: a strict environment must not need a CDN.
            assert "http://" not in response.text.replace("http://127.0.0.1", "")
            assert "<script" in response.text

    def test_index_advertises_the_ui_when_enabled(self):
        harness = Harness(ui_enabled=True)
        with harness.client() as client:
            body = wire.ServiceIndex.model_validate(client.get("/").json())

            assert body.ui_url == "/ui"

    def test_ui_needs_no_token_but_its_api_calls_do(self):
        """The page itself is static; every call it makes is authenticated."""
        harness = Harness(ui_enabled=True)
        with harness.client() as client:
            assert client.get("/ui").status_code == 200
            assert client.get("/sessions").status_code == 401


class TestHealth:
    """The unauthenticated probe endpoint."""

    def test_health_needs_no_token(self, client: TestClient):
        response = client.get("/healthz")

        assert response.status_code == 200
        body = wire.ServiceHealth.model_validate(response.json())
        assert body.status == "ok"
        assert body.sessions == 0
        assert body.runtimes == ["node", "python"]


class TestAuthentication:
    """Token handling — the boundary that keeps tenants apart."""

    def test_opening_a_session_requires_the_service_token(self, client: TestClient):
        assert client.post("/sessions", json={}).status_code == 401
        assert (
            client.post("/sessions", json={}, headers={wire.TOKEN_HEADER: "wrong"}).status_code
            == 401
        )

    def test_listing_requires_the_service_token(self, client: TestClient):
        assert client.get("/sessions").status_code == 401

    def test_operations_require_a_token(self, client: TestClient):
        session_id, _ = _open_session(client)

        response = client.post(f"/sessions/{session_id}/exec", json={"command": "echo hi"})

        assert response.status_code == 401

    def test_a_session_token_cannot_reach_another_session(self, client: TestClient):
        first, first_token = _open_session(client, session_id="tenant-a")
        second, _ = _open_session(client, session_id="tenant-b")

        response = client.post(
            f"/sessions/{second}/exec",
            json={"command": "cat /etc/shadow"},
            headers={wire.TOKEN_HEADER: first_token},
        )

        assert response.status_code == 401
        assert first != second

    def test_the_service_token_reaches_every_session(self, client: TestClient):
        session_id, _ = _open_session(client, session_id="tenant-a")

        response = client.post(
            f"/sessions/{session_id}/exec",
            json={"command": "echo hi"},
            headers=_service_headers(),
        )

        assert response.status_code == 200

    def test_unknown_session_with_a_valid_service_token_is_404(self, client: TestClient):
        response = client.get("/sessions/nope", headers=_service_headers())

        assert response.status_code == 404

    def test_unknown_session_without_a_token_is_401_not_404(self, client: TestClient):
        """Existence must not leak to an unauthenticated caller."""
        assert client.get("/sessions/nope").status_code == 401


class TestSessionLifecycle:
    """Opening, inspecting, listing and closing sessions."""

    def test_open_generates_an_id_and_starts_the_sandbox(
        self, client: TestClient, harness: Harness
    ):
        session_id, token = _open_session(client)

        assert session_id.startswith("s-")
        assert token
        assert harness.built[session_id].started == 1
        assert harness.built[session_id].alive is True

    def test_open_honours_a_supplied_id_and_runtime(self, client: TestClient, harness: Harness):
        session_id, _ = _open_session(client, session_id="my-session", runtime="node")

        assert session_id == "my-session"
        assert harness.built["my-session"].image == "node:20-slim"

    def test_open_rejects_a_runtime_outside_the_allowlist(self, client: TestClient):
        """A client naming its own image would be a host takeover."""
        response = client.post("/sessions", json={"runtime": "evil"}, headers=_service_headers())

        assert response.status_code == 400
        assert "Unknown runtime" in response.json()["detail"]

    def test_open_rejects_a_traversing_session_id(self, client: TestClient):
        response = client.post(
            "/sessions", json={"session_id": "../../etc/passwd"}, headers=_service_headers()
        )

        assert response.status_code == 422

    def test_open_conflicts_on_a_duplicate_id(self, client: TestClient):
        _open_session(client, session_id="taken")

        response = client.post(
            "/sessions", json={"session_id": "taken"}, headers=_service_headers()
        )

        assert response.status_code == 409

    def test_open_reports_capacity_with_429(self):
        harness = Harness(max_sessions=1)
        with harness.client() as client:
            _open_session(client, session_id="first")

            response = client.post(
                "/sessions", json={"session_id": "second"}, headers=_service_headers()
            )

            assert response.status_code == 429
            assert "Session limit" in response.json()["detail"]

    def test_open_reports_a_failed_sandbox_start_with_502(self, harness: Harness):
        harness.next_start_error = RuntimeError("daemon down")
        with harness.client() as client:
            response = client.post("/sessions", json={}, headers=_service_headers())

            assert response.status_code == 502
            assert "daemon down" in response.json()["detail"]

    def test_a_failed_open_does_not_consume_capacity(self, harness: Harness):
        harness.next_start_error = RuntimeError("daemon down")
        with harness.client() as client:
            client.post("/sessions", json={}, headers=_service_headers())
            harness.next_start_error = None

            session_id, _ = _open_session(client)
            assert harness.built[session_id].alive is True

    def test_inspect_reports_liveness_and_idleness(self, client: TestClient):
        session_id, token = _open_session(client, session_id="watched")

        response = client.get(f"/sessions/{session_id}", headers={wire.TOKEN_HEADER: token})

        info = wire.SessionInfo.model_validate(response.json())
        assert info.session_id == "watched"
        assert info.runtime == "python"
        assert info.alive is True
        assert info.idle_seconds >= 0
        assert info.usage is None

    def test_inspect_includes_usage_on_request(self, client: TestClient, harness: Harness):
        session_id, _ = _open_session(client)
        harness.built[session_id].usage = SandboxUsage(
            memory_bytes=1024, memory_limit_bytes=4096, cpu_percent=12.5, pids=7
        )

        response = client.get(
            f"/sessions/{session_id}", params={"usage": "true"}, headers=_service_headers()
        )

        info = wire.SessionInfo.model_validate(response.json())
        assert info.usage is not None
        assert info.usage.memory_bytes == 1024
        assert info.usage.cpu_percent == 12.5
        assert info.usage.pids == 7

    def test_listing_shows_open_sessions_and_the_limit(self, client: TestClient):
        _open_session(client, session_id="one")
        _open_session(client, session_id="two")

        response = client.get("/sessions", headers=_service_headers())

        listing = wire.SessionList.model_validate(response.json())
        assert {s.session_id for s in listing.sessions} == {"one", "two"}
        assert listing.limit == 20
        assert all(s.usage is None for s in listing.sessions)

    def test_listing_can_sample_usage(self, client: TestClient, harness: Harness):
        session_id, _ = _open_session(client)
        harness.built[session_id].usage = SandboxUsage(memory_bytes=99)

        response = client.get("/sessions", params={"usage": "true"}, headers=_service_headers())

        listing = wire.SessionList.model_validate(response.json())
        assert listing.sessions[0].usage is not None
        assert listing.sessions[0].usage.memory_bytes == 99

    def test_delete_stops_the_sandbox_and_forgets_it(self, client: TestClient, harness: Harness):
        session_id, token = _open_session(client)

        response = client.delete(f"/sessions/{session_id}", headers={wire.TOKEN_HEADER: token})

        assert response.status_code == 204
        assert harness.built[session_id].stopped == 1
        assert client.get(f"/sessions/{session_id}", headers=_service_headers()).status_code == 404

    def test_shutdown_releases_every_session(self, harness: Harness):
        with harness.client() as client:
            first, _ = _open_session(client, session_id="one")
            second, _ = _open_session(client, session_id="two")

        assert harness.built[first].stopped == 1
        assert harness.built[second].stopped == 1


class TestOperationsOverHttp:
    """The file and command endpoints, driven directly."""

    def test_exec_is_capped_by_the_service_timeout(self, harness: Harness):
        harness = Harness(execute_timeout=30)
        with harness.client() as client:
            session_id, _ = _open_session(client)

            client.post(
                f"/sessions/{session_id}/exec",
                json={"command": "sleep 600", "timeout_seconds": 9999},
                headers=_service_headers(),
            )

            assert harness.built[session_id].commands == [("sleep 600", 30)]

    def test_exec_without_a_timeout_uses_the_service_ceiling(self, harness: Harness):
        harness = Harness(execute_timeout=45)
        with harness.client() as client:
            session_id, _ = _open_session(client)

            client.post(
                f"/sessions/{session_id}/exec",
                json={"command": "echo hi"},
                headers=_service_headers(),
            )

            assert harness.built[session_id].commands == [("echo hi", 45)]

    def test_exec_keeps_a_shorter_client_timeout(self, harness: Harness):
        harness = Harness(execute_timeout=300)
        with harness.client() as client:
            session_id, _ = _open_session(client)

            client.post(
                f"/sessions/{session_id}/exec",
                json={"command": "echo hi", "timeout_seconds": 5},
                headers=_service_headers(),
            )

            assert harness.built[session_id].commands == [("echo hi", 5)]

    def test_write_rejects_content_that_is_not_base64(self, client: TestClient):
        session_id, _ = _open_session(client)

        response = client.post(
            f"/sessions/{session_id}/write",
            json={"path": "/f.txt", "content_b64": "not base64!!"},
            headers=_service_headers(),
        )

        assert response.status_code == 400
        assert "base64" in response.json()["detail"]

    def test_grep_reports_a_search_error(self, client: TestClient, harness: Harness):
        session_id, _ = _open_session(client)
        harness.built[session_id].grep_raw = lambda *a, **k: "Error: bad pattern"  # type: ignore[method-assign]

        response = client.post(
            f"/sessions/{session_id}/grep",
            json={"pattern": "["},
            headers=_service_headers(),
        )

        body = wire.GrepResponse.model_validate(response.json())
        assert body.error == "Error: bad pattern"
        assert body.matches == []

    def test_usage_is_absent_for_a_sandbox_that_cannot_report_it(self):
        """A backend with no `resource_usage` simply reports nothing."""
        from pydantic_ai_backends.remote.server import _usage_of

        class Bare:
            pass

        assert _usage_of(Bare()) is None

    def test_usage_is_absent_when_the_sampler_returns_junk(
        self, client: TestClient, harness: Harness
    ):
        session_id, _ = _open_session(client)
        harness.built[session_id].resource_usage = lambda: "not usage"  # type: ignore[method-assign]

        response = client.get(
            f"/sessions/{session_id}", params={"usage": "true"}, headers=_service_headers()
        )

        assert wire.SessionInfo.model_validate(response.json()).usage is None


class TestRemoteSandboxAgainstService:
    """End-to-end: the real client driving the real service."""

    @pytest.fixture
    def sandbox(self, client: TestClient):
        remote = RemoteSandbox(token=SERVICE_TOKEN, session_id="e2e", client=client)
        remote.start()
        yield remote
        remote.stop()

    def test_start_is_idempotent(self, sandbox: RemoteSandbox, harness: Harness):
        sandbox.start()

        assert harness.built["e2e"].started == 1

    def test_execute_round_trip(self, sandbox: RemoteSandbox):
        result = sandbox.execute("echo hi", timeout=5)

        assert result.output == "ran echo hi"
        assert result.exit_code == 0
        assert result.truncated is False

    def test_write_read_and_edit(self, sandbox: RemoteSandbox):
        assert sandbox.write("/app.py", "value = 'old'\n").error is None
        assert "value = 'old'" in sandbox.read("/app.py")

        edited = sandbox.edit("/app.py", "old", "new")
        assert edited.error is None
        assert edited.occurrences == 1
        assert "value = 'new'" in sandbox.read("/app.py")

    def test_write_and_read_bytes_survives_non_utf8(self, sandbox: RemoteSandbox):
        payload = bytes(range(256))

        assert sandbox.write("/blob.bin", payload).error is None
        assert sandbox.read_bytes("/blob.bin") == payload

    def test_exists(self, sandbox: RemoteSandbox):
        sandbox.write("/there.txt", "x")

        assert sandbox.exists("/there.txt") is True
        assert sandbox.exists("/missing.txt") is False

    def test_ls_and_glob(self, sandbox: RemoteSandbox):
        sandbox.write("/pkg/a.py", "a")
        sandbox.write("/pkg/b.txt", "b")

        names = {entry["name"] for entry in sandbox.ls_info("/pkg")}
        assert names == {"a.py", "b.txt"}

        matched = sandbox.glob_info("**/*.py", "/")
        assert [entry["path"] for entry in matched] == ["/pkg/a.py"]

    def test_grep_returns_matches(self, sandbox: RemoteSandbox):
        sandbox.write("/notes.txt", "alpha\nbeta\n")

        found = sandbox.grep_raw("beta")

        assert isinstance(found, list)
        assert found[0]["line_number"] == 2
        assert found[0]["line"] == "beta"

    def test_is_alive_tracks_the_remote_session(self, sandbox: RemoteSandbox, harness: Harness):
        assert sandbox.is_alive() is True

        harness.built["e2e"].alive = False
        assert sandbox.is_alive() is False

    def test_resource_usage_round_trip(self, sandbox: RemoteSandbox, harness: Harness):
        harness.built["e2e"].usage = SandboxUsage(memory_bytes=2048, cpu_percent=3.5)

        usage = sandbox.resource_usage()

        assert usage is not None
        assert usage.memory_bytes == 2048
        assert usage.cpu_percent == 3.5

    def test_resource_usage_is_none_when_unavailable(
        self, sandbox: RemoteSandbox, harness: Harness
    ):
        harness.built["e2e"].usage = None

        assert sandbox.resource_usage() is None

    def test_stop_deletes_the_remote_session(self, client: TestClient, harness: Harness):
        remote = RemoteSandbox(token=SERVICE_TOKEN, session_id="tostop", client=client)
        remote.start()
        remote.stop()

        assert harness.built["tostop"].stopped == 1
        assert client.get("/sessions/tostop", headers=_service_headers()).status_code == 404

    def test_stop_before_start_is_a_no_op(self, client: TestClient):
        RemoteSandbox(token=SERVICE_TOKEN, client=client).stop()

    def test_session_manager_can_drive_remote_sandboxes(self, client: TestClient, harness: Harness):
        """RemoteSandbox is a drop-in for SessionManager's factory contract."""
        from pydantic_ai_backends import SessionManager

        manager = SessionManager(
            sandbox_factory=lambda sid: RemoteSandbox(
                token=SERVICE_TOKEN, session_id=sid, client=client
            )
        )

        async def scenario() -> str:
            sandbox = await manager.get_or_create("via-manager")
            output = sandbox.execute("echo managed").output
            await manager.shutdown()
            return str(output)

        import asyncio

        assert asyncio.run(scenario()) == "ran echo managed"
        assert harness.built["via-manager"].stopped == 1


class TestRemoteSandboxFailureHandling:
    """A broken service must degrade, not raise, on operations."""

    @pytest.fixture
    def offline(self, client: TestClient):
        """A started sandbox whose session the service has since forgotten."""
        remote = RemoteSandbox(token=SERVICE_TOKEN, session_id="gone", client=client)
        remote.start()
        client.delete("/sessions/gone", headers=_service_headers())
        return remote

    def test_operations_degrade_on_a_missing_session(self, offline: RemoteSandbox):
        assert offline.read("/f.txt").startswith("Error:")
        assert offline.read_bytes("/f.txt") == b""
        assert offline.write("/f.txt", "x").error is not None
        assert offline.edit("/f.txt", "a", "b").error is not None
        assert offline.exists("/f.txt") is False
        assert offline.ls_info("/") == []
        assert offline.glob_info("*.py") == []
        assert offline.grep_raw("x") == "Error: could not search for 'x'"
        assert offline.execute("echo hi").exit_code == 1
        assert offline.is_alive() is False
        assert offline.resource_usage() is None

    def test_start_raises_when_the_service_refuses(self, client: TestClient):
        remote = RemoteSandbox(token="wrong-token", client=client)

        with pytest.raises(RuntimeError, match="refused to open a session"):
            remote.start()

    def test_start_raises_when_the_service_is_unreachable(self):
        class Dead:
            def post(self, *args: Any, **kwargs: Any) -> Any:
                raise OSError("connection refused")

        remote = RemoteSandbox(client=Dead())  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="Could not reach the sandbox service"):
            remote.start()

    def test_transport_failures_are_swallowed_by_operations(self):
        class Dead:
            def post(self, *args: Any, **kwargs: Any) -> Any:
                raise OSError("connection refused")

            def get(self, *args: Any, **kwargs: Any) -> Any:
                raise OSError("connection refused")

            def delete(self, *args: Any, **kwargs: Any) -> Any:
                raise OSError("connection refused")

        remote = RemoteSandbox(client=Dead())  # type: ignore[arg-type]

        assert remote.execute("echo hi").exit_code == 1
        assert remote.read("/f.txt").startswith("Error:")
        assert remote.is_alive() is False
        assert remote.resource_usage() is None
        remote.stop()

    def test_undecodable_read_bytes_payload_yields_empty(self, client: TestClient):
        remote = RemoteSandbox(token=SERVICE_TOKEN, session_id="bad64", client=client)
        remote.start()

        class Corrupt:
            status_code = 200

            @staticmethod
            def json() -> dict[str, str]:
                return {"content_b64": "!!!not base64!!!"}

        remote._post = lambda *a, **k: Corrupt()  # type: ignore[method-assign]

        assert remote.read_bytes("/f.txt") == b""

    def test_owned_client_is_built_and_closed(self):
        """Without an injected client, RemoteSandbox owns an httpx client."""
        remote = RemoteSandbox("http://localhost:9/", token="t")

        assert remote._owns_client is True
        remote.stop()
        assert remote._http.is_closed


class TestRemoteSandboxConstruction:
    """Client construction details that do not need a service."""

    def test_generated_session_id_is_used(self):
        remote = RemoteSandbox(client=object())  # type: ignore[arg-type]

        assert remote.session_id.startswith("remote-")
        assert remote.session_id == remote.id

    def test_base_url_trailing_slash_is_trimmed(self):
        remote = RemoteSandbox("http://example.test:8080/", token="t")
        try:
            assert str(remote._http.base_url) == "http://example.test:8080"
        finally:
            remote.stop()


class TestWireModels:
    """Protocol-level details worth pinning."""

    def test_session_id_pattern_rejects_traversal(self):
        import re

        assert re.match(wire.SESSION_ID_PATTERN, "user-123.v2") is not None
        assert re.match(wire.SESSION_ID_PATTERN, "../etc") is None
        assert re.match(wire.SESSION_ID_PATTERN, "a/b") is None
        assert re.match(wire.SESSION_ID_PATTERN, "") is None

    def test_create_session_request_rejects_a_bad_id(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            wire.CreateSessionRequest(session_id="../escape")

    def test_write_request_carries_base64(self):
        body = wire.WriteRequest(path="/f", content_b64=base64.b64encode(b"\x00\xff").decode())

        assert base64.b64decode(body.content_b64) == b"\x00\xff"


class TestServiceInternals:
    """Paths not reachable through a plain request/response cycle."""

    def test_default_builder_applies_every_configured_ceiling(self):
        """The client supplies none of this — that is the security property."""
        from pydantic_ai_backends.remote.server import _default_builder

        config = SandboxdConfig(
            token="t",
            runtimes={"python": "python:3.12-slim"},
            work_dir="/srv",
            network_mode="none",
            mem_limit="256m",
            cpus=0.5,
            pids_limit=64,
            idle_timeout=120,
            max_read_bytes=4096,
        )

        from pydantic_ai_backends.remote.server import SandboxRuntime

        sandbox = _default_builder(config)("sess-1", SandboxRuntime(image="python:3.12-slim"))

        assert sandbox._image == "python:3.12-slim"
        assert sandbox.session_id == "sess-1"
        assert sandbox._work_dir == "/srv"
        assert sandbox._network_mode == "none"
        assert sandbox._mem_limit == "256m"
        assert sandbox._cpus == 0.5
        assert sandbox._pids_limit == 64
        assert sandbox._idle_timeout == 120
        assert sandbox._max_read_bytes == 4096

    async def test_shutdown_without_startup_is_safe(self):
        """`shutdown` runs even if the lifespan never created a pool."""
        from pydantic_ai_backends.remote.server import _Service

        service = _Service(
            SandboxdConfig(token="t", runtimes={"python": "img"}),
            lambda sid, image: FakeSandbox(sid, image),
        )

        await service.shutdown()

    def test_peek_refuses_a_session_without_a_sandbox(self):
        """Inspection must not create anything, so a gap is a 404."""
        from fastapi import HTTPException

        from pydantic_ai_backends.remote.server import _Service

        service = _Service(
            SandboxdConfig(token="t", runtimes={"python": "img"}),
            lambda sid, image: FakeSandbox(sid, image),
        )

        with pytest.raises(HTTPException) as excinfo:
            service.peek("ghost")

        assert excinfo.value.status_code == 404

    def test_inspect_does_not_revive_a_dead_sandbox(self, client: TestClient, harness: Harness):
        """An operator asking about a dead session must be told it is dead."""
        session_id, _ = _open_session(client, session_id="dead")
        harness.built[session_id].alive = False

        response = client.get(f"/sessions/{session_id}", headers=_service_headers())

        assert wire.SessionInfo.model_validate(response.json()).alive is False
        assert harness.built[session_id].started == 1

    def test_operations_do_revive_a_dead_sandbox(self, client: TestClient, harness: Harness):
        """A client asking to run something wants a working sandbox."""
        session_id, _ = _open_session(client, session_id="healme")
        harness.built[session_id].alive = False

        response = client.post(
            f"/sessions/{session_id}/exec",
            json={"command": "echo hi"},
            headers=_service_headers(),
        )

        assert response.status_code == 200
        assert harness.built[session_id].alive is True

    def test_sandbox_lookup_404s_when_bookkeeping_is_gone(self):
        """A session known to auth but with no pending image cannot be built."""
        from fastapi import HTTPException

        from pydantic_ai_backends.remote.server import _Service

        service = _Service(
            SandboxdConfig(token="t", runtimes={"python": "img"}),
            lambda sid, image: FakeSandbox(sid, image),
        )

        async def scenario() -> int:
            try:
                await service.sandbox("orphan")
            except HTTPException as exc:
                return exc.status_code
            return 0

        import asyncio

        assert asyncio.run(scenario()) == 404


class TestClientGrepParsing:
    """The grep response has two shapes; both are exercised end to end."""

    def test_matches_are_mapped_to_grep_match_rows(self, client: TestClient):
        remote = RemoteSandbox(token=SERVICE_TOKEN, session_id="grepper", client=client)
        remote.start()
        try:
            remote.write("/a.txt", "one\ntwo\nthree\n")
            found = remote.grep_raw("t")

            assert isinstance(found, list)
            assert [row["line"] for row in found] == ["two", "three"]
            assert all(row["path"] == "/a.txt" for row in found)
        finally:
            remote.stop()

    def test_client_surfaces_a_server_side_grep_error(self, client: TestClient, harness: Harness):
        """`grep_raw`'s string branch has to survive the round trip."""
        remote = RemoteSandbox(token=SERVICE_TOKEN, session_id="greperr", client=client)
        remote.start()
        try:
            harness.built["greperr"].grep_raw = (  # type: ignore[method-assign]
                lambda *a, **k: "Error: unterminated character class"
            )

            assert remote.grep_raw("[") == "Error: unterminated character class"
        finally:
            remote.stop()


class TestServicePolicy:
    """The policy endpoint the dashboard reads its limits from."""

    def test_policy_requires_the_service_token(self, client: TestClient):
        assert client.get("/policy").status_code == 401

    def test_policy_reports_the_enforced_ceilings(self):
        harness = Harness(
            mem_limit="256m",
            cpus=0.5,
            pids_limit=64,
            network_mode="none",
            work_dir="/srv",
            idle_timeout=120,
            execute_timeout=30,
            max_read_bytes=4096,
            max_sessions=3,
        )
        with harness.client() as client:
            body = wire.ServicePolicy.model_validate(
                client.get("/policy", headers=_service_headers()).json()
            )

        assert body.mem_limit == "256m"
        assert body.cpus == 0.5
        assert body.pids_limit == 64
        assert body.network_mode == "none"
        assert body.work_dir == "/srv"
        assert body.idle_timeout == 120
        assert body.execute_timeout == 30
        assert body.max_read_bytes == 4096
        assert body.max_sessions == 3
        assert body.default_runtime == "python"
        assert [r.alias for r in body.runtimes] == ["node", "python"]
        assert [r.image for r in body.runtimes] == ["node:20-slim", "python:3.12-slim"]

    def test_unlimited_ceilings_are_reported_as_null(self):
        harness = Harness(mem_limit=None, cpus=None, pids_limit=None, network_mode=None)
        with harness.client() as client:
            body = wire.ServicePolicy.model_validate(
                client.get("/policy", headers=_service_headers()).json()
            )

        assert body.mem_limit is None
        assert body.cpus is None
        assert body.pids_limit is None
        assert body.network_mode is None


class TestSessionActivity:
    """The per-session operation log the dashboard's Activity tab reads."""

    def _events(self, client: TestClient, session_id: str, after: int = 0):
        response = client.get(
            f"/sessions/{session_id}/events",
            params={"after": after},
            headers=_service_headers(),
        )
        assert response.status_code == 200, response.text
        return wire.SessionEvents.model_validate(response.json())

    def test_a_fresh_session_has_no_activity(self, client: TestClient):
        session_id, _ = _open_session(client)

        log = self._events(client, session_id)

        assert log.events == []
        assert log.latest_seq == 0

    def test_operations_are_recorded_in_order(self, client: TestClient):
        session_id, _ = _open_session(client)
        headers = _service_headers()
        base = f"/sessions/{session_id}"

        client.post(
            f"{base}/write", json={"path": "/a.txt", "content_b64": "aGk="}, headers=headers
        )
        client.post(f"{base}/exec", json={"command": "echo hi"}, headers=headers)
        client.post(f"{base}/ls", json={"path": "/"}, headers=headers)

        log = self._events(client, session_id)

        assert [event.op for event in log.events] == ["write", "exec", "ls"]
        assert [event.seq for event in log.events] == [1, 2, 3]
        assert log.latest_seq == 3
        assert all(event.ok for event in log.events)
        assert all(event.duration_ms >= 0 for event in log.events)

    def test_the_log_records_targets_and_outcomes_not_payloads(self, client: TestClient):
        """An audit trail that stored contents would be a data leak."""
        session_id, _ = _open_session(client)
        secret = base64.b64encode(b"super-secret-content").decode()
        client.post(
            f"/sessions/{session_id}/write",
            json={"path": "/secrets.txt", "content_b64": secret},
            headers=_service_headers(),
        )

        event = self._events(client, session_id).events[0]

        assert event.target == "/secrets.txt"
        assert event.detail == "20 bytes"
        assert "super-secret" not in event.model_dump_json()

    def test_a_failing_operation_is_recorded_as_not_ok(self, client: TestClient, harness: Harness):
        session_id, _ = _open_session(client)
        harness.built[session_id].execute = lambda command, timeout=None: ExecuteResponse(
            output="boom", exit_code=1
        )

        client.post(
            f"/sessions/{session_id}/exec",
            json={"command": "false"},
            headers=_service_headers(),
        )

        event = self._events(client, session_id).events[0]
        assert event.ok is False
        assert event.detail == "exit 1"

    def test_a_raising_operation_is_still_recorded(self, client: TestClient, harness: Harness):
        """The case an operator most wants to see must not be the one that is lost."""
        session_id, _ = _open_session(client)

        def explode(command, timeout=None):
            raise RuntimeError("daemon vanished")

        harness.built[session_id].execute = explode

        # TestClient re-raises server exceptions rather than surfacing the 500,
        # so the point here is that the entry survives the failure either way.
        with pytest.raises(RuntimeError, match="daemon vanished"):
            client.post(
                f"/sessions/{session_id}/exec",
                json={"command": "boom"},
                headers=_service_headers(),
            )

        event = self._events(client, session_id).events[0]
        assert event.op == "exec"
        assert event.target == "boom"
        assert event.ok is False

    def test_after_returns_only_newer_entries(self, client: TestClient):
        session_id, _ = _open_session(client)
        for index in range(3):
            client.post(
                f"/sessions/{session_id}/exists",
                json={"path": f"/f{index}"},
                headers=_service_headers(),
            )

        tail = self._events(client, session_id, after=2)

        assert [event.seq for event in tail.events] == [3]
        assert tail.latest_seq == 3

    def test_long_targets_are_truncated(self, client: TestClient):
        session_id, _ = _open_session(client)
        client.post(
            f"/sessions/{session_id}/exec",
            json={"command": "echo " + "x" * 500},
            headers=_service_headers(),
        )

        event = self._events(client, session_id).events[0]
        assert len(event.target) <= 201
        assert event.target.endswith("…")

    def test_history_is_bounded(self, client: TestClient, harness: Harness):
        """A long-lived session must not grow the service's memory forever."""
        from pydantic_ai_backends.remote.server import _EVENT_HISTORY

        session_id, _ = _open_session(client)
        service = harness.app.state.service
        for _ in range(_EVENT_HISTORY + 25):
            with service.observe(session_id, "exec", "noop") as outcome:
                outcome.ok = True

        log = self._events(client, session_id)
        assert len(log.events) == _EVENT_HISTORY
        assert log.latest_seq == _EVENT_HISTORY + 25

    def test_activity_needs_authorization(self, client: TestClient):
        session_id, _ = _open_session(client)

        assert client.get(f"/sessions/{session_id}/events").status_code == 401

    def test_a_session_token_reads_only_its_own_activity(self, client: TestClient):
        first, first_token = _open_session(client, session_id="tenant-a")
        _open_session(client, session_id="tenant-b")

        allowed = client.get(f"/sessions/{first}/events", headers={wire.TOKEN_HEADER: first_token})
        denied = client.get("/sessions/tenant-b/events", headers={wire.TOKEN_HEADER: first_token})

        assert allowed.status_code == 200
        assert denied.status_code == 401

    def test_events_for_an_untracked_session_are_not_recorded(self, harness: Harness):
        """`observe` is a no-op once the session is gone, rather than a KeyError."""
        with harness.client() as client:
            _open_session(client, session_id="doomed")
            service = harness.app.state.service
            client.delete("/sessions/doomed", headers=_service_headers())

            with service.observe("doomed", "exec", "after the fact") as outcome:
                outcome.ok = True

            assert (
                client.get("/sessions/doomed/events", headers=_service_headers()).status_code == 404
            )


class TestSessionReuse:
    """A sandbox that must outlive the run which created it."""

    def test_a_colliding_id_is_refused_by_default(self, client: TestClient):
        """Silently sharing another caller's sandbox is the worse failure."""
        session_id, _ = _open_session(client, session_id="shared")

        response = client.post(
            "/sessions", json={"session_id": session_id}, headers=_service_headers()
        )

        assert response.status_code == 409
        assert "Session exists" in response.text

    def test_reuse_attaches_to_the_open_session(self, harness: Harness):
        with harness.client() as client:
            session_id, token = _open_session(client, session_id="conv-1")

            response = client.post(
                "/sessions",
                json={"session_id": session_id, "reuse": True},
                headers=_service_headers(),
            )

            assert response.status_code == 200
            attached = wire.SessionCreated.model_validate(response.json())
            # The same sandbox, and the token the first caller still holds.
            assert attached.session.session_id == session_id
            assert attached.token == token
            assert harness.built[session_id].started == 1

    def test_reuse_sees_the_files_the_first_run_wrote(self, client: TestClient):
        first = RemoteSandbox(token=SERVICE_TOKEN, session_id="conv-2", client=client)
        first.start()
        first.write("/notes.txt", "from the first run")

        second = RemoteSandbox(token=SERVICE_TOKEN, session_id="conv-2", reuse=True, client=client)
        second.start()

        assert second.read_bytes("/notes.txt") == b"from the first run"

    def test_reuse_without_an_open_session_creates_one(self, client: TestClient):
        sandbox = RemoteSandbox(token=SERVICE_TOKEN, session_id="conv-3", reuse=True, client=client)

        sandbox.start()

        assert sandbox.is_alive() is True

    def test_attaching_with_a_different_runtime_is_refused(self, client: TestClient):
        """Honouring it would replace a live sandbox and drop its files."""
        session_id, _ = _open_session(client, session_id="conv-4", runtime="python")

        response = client.post(
            "/sessions",
            json={"session_id": session_id, "runtime": "node", "reuse": True},
            headers=_service_headers(),
        )

        assert response.status_code == 409
        assert "Close it before changing runtime" in response.text


class TestReapedSessionsAreForgotten:
    """Idle cleanup must not leave bookkeeping behind."""

    async def test_reaping_drops_the_session_record(self, harness: Harness):
        with harness.client() as client:
            session_id, _ = _open_session(client, session_id="idle-1")
            service = harness.app.state.service

            assert await service.manager.cleanup_idle(max_idle=0) == 1

            assert session_id not in service._sessions
            assert service.health().sessions == 0

    async def test_reuse_after_reaping_opens_a_fresh_sandbox(self, harness: Harness):
        with harness.client() as client:
            session_id, _ = _open_session(client, session_id="idle-2")
            service = harness.app.state.service
            reaped = harness.built[session_id]
            await service.manager.cleanup_idle(max_idle=0)

            response = client.post(
                "/sessions",
                json={"session_id": session_id, "reuse": True},
                headers=_service_headers(),
            )

            assert response.status_code == 200
            # A new sandbox, not the reaped one revived — and its files are gone,
            # which is exactly why `workspace_root` exists.
            assert harness.built[session_id] is not reaped
            assert harness.built[session_id].alive is True


class TestWorkspacePersistence:
    """`workspace_root` gives a session files that survive its container."""

    def test_no_workspace_root_mounts_nothing(self):
        from pydantic_ai_backends.remote.server import SandboxdConfig, _session_volumes

        config = SandboxdConfig(token="t")

        assert _session_volumes(config, "s1") is None

    def test_each_session_gets_its_own_directory(self, tmp_path):
        from pydantic_ai_backends.remote.server import SandboxdConfig, _session_volumes

        config = SandboxdConfig(token="t", workspace_root=str(tmp_path))

        volumes = _session_volumes(config, "s1")

        assert volumes == {str((tmp_path / "s1" / "workspace").resolve()): "/workspace"}
        assert (tmp_path / "s1" / "workspace").is_dir()
        assert _session_volumes(config, "s2") != volumes


class TestPerTenantCapacity:
    """One tenant must not be able to occupy the whole pool."""

    def test_a_tenant_is_refused_at_its_own_ceiling(self):
        harness = Harness(max_sessions_per_tenant=2)
        with harness.client() as client:
            for index in range(2):
                _open_session(client, session_id=f"a-{index}", tenant="org-a")

            response = client.post(
                "/sessions",
                json={"session_id": "a-2", "tenant": "org-a"},
                headers=_service_headers(),
            )

            assert response.status_code == 429
            assert "org-a" in response.text

    def test_another_tenant_still_gets_a_session(self):
        harness = Harness(max_sessions_per_tenant=1)
        with harness.client() as client:
            _open_session(client, session_id="a-0", tenant="org-a")

            response = client.post(
                "/sessions",
                json={"session_id": "b-0", "tenant": "org-b"},
                headers=_service_headers(),
            )

            assert response.status_code == 200

    def test_releasing_frees_the_tenant_slot(self):
        harness = Harness(max_sessions_per_tenant=1)
        with harness.client() as client:
            _open_session(client, session_id="a-0", tenant="org-a")
            client.delete("/sessions/a-0", headers=_service_headers())

            response = client.post(
                "/sessions",
                json={"session_id": "a-1", "tenant": "org-a"},
                headers=_service_headers(),
            )

            assert response.status_code == 200

    def test_unlabelled_sessions_are_not_counted(self):
        """A client that declares no tenant is subject only to the global cap."""
        harness = Harness(max_sessions_per_tenant=1)
        with harness.client() as client:
            _open_session(client, session_id="a-0")

            response = client.post(
                "/sessions", json={"session_id": "a-1"}, headers=_service_headers()
            )

            assert response.status_code == 200

    def test_the_tenant_is_reported_back(self, client: TestClient):
        session_id, _ = _open_session(client, session_id="a-0", tenant="org-a")

        listed = wire.SessionList.model_validate(
            client.get("/sessions", headers=_service_headers()).json()
        )

        assert [s.tenant for s in listed.sessions if s.session_id == session_id] == ["org-a"]

    def test_the_ceiling_is_visible_in_the_policy(self):
        harness = Harness(max_sessions_per_tenant=3)
        with harness.client() as client:
            policy = wire.ServicePolicy.model_validate(
                client.get("/policy", headers=_service_headers()).json()
            )

        assert policy.max_sessions_per_tenant == 3


class TestPersistedContainers:
    """A stable container name is what makes installed packages survive."""

    def test_off_by_default(self):
        from pydantic_ai_backends.remote.server import SandboxdConfig, _container_name

        assert _container_name(SandboxdConfig(token="t"), "s1") is None

    def test_named_after_the_session_when_enabled(self):
        from pydantic_ai_backends.remote.server import SandboxdConfig, _container_name

        config = SandboxdConfig(token="t", persist_containers=True)

        assert _container_name(config, "org-conv1") == "sandboxd-org-conv1"

    def test_the_setting_is_visible_in_the_policy(self):
        harness = Harness(persist_containers=True)
        with harness.client() as client:
            policy = wire.ServicePolicy.model_validate(
                client.get("/policy", headers=_service_headers()).json()
            )

        assert policy.persist_containers is True


class TestPurge:
    """Closing a session can discard everything it accumulated."""

    def test_purge_removes_the_workspace(self, tmp_path):
        harness = Harness(workspace_root=str(tmp_path))
        with harness.client() as client:
            _open_session(client, session_id="gone")
            workspace = tmp_path / "gone"
            assert workspace.is_dir()

            client.delete("/sessions/gone", params={"purge": "true"}, headers=_service_headers())

            assert not workspace.exists()

    def test_a_plain_close_keeps_the_workspace(self, tmp_path):
        """That is what lets a later attach find the same files."""
        harness = Harness(workspace_root=str(tmp_path))
        with harness.client() as client:
            _open_session(client, session_id="kept")

            client.delete("/sessions/kept", headers=_service_headers())

            assert (tmp_path / "kept" / "workspace").is_dir()

    def test_purge_asks_the_sandbox_to_remove_itself(self, harness: Harness):
        with harness.client() as client:
            _open_session(client, session_id="removed")
            sandbox = harness.built["removed"]

            client.delete("/sessions/removed", params={"purge": "true"}, headers=_service_headers())

            assert sandbox.removed is True

    def test_purge_tolerates_a_sandbox_that_cannot_remove(self, harness: Harness):
        """The base sandbox surface has `stop()` with no `remove` argument."""
        with harness.client() as client:
            _open_session(client, session_id="plain")
            sandbox = harness.built["plain"]
            sandbox.accepts_remove = False

            response = client.delete(
                "/sessions/plain", params={"purge": "true"}, headers=_service_headers()
            )

            assert response.status_code == 204
            assert sandbox.alive is False
            assert sandbox.removed is False


class TestWorkspaceSweep:
    """Workspaces outlive their sessions, so something has to reclaim them."""

    def _config(self, tmp_path, **kwargs):
        from pydantic_ai_backends.remote.server import SandboxdConfig

        return SandboxdConfig(token="t", workspace_root=str(tmp_path), **kwargs)

    def _aged(self, tmp_path, name: str, age: float) -> Path:
        directory = tmp_path / name
        (directory / "workspace").mkdir(parents=True)
        os.utime(directory, (0, 1_000_000 - age))
        return directory

    def test_nothing_is_swept_without_a_ttl(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_workspaces

        old = self._aged(tmp_path, "old", age=10_000)

        assert sweep_workspaces(self._config(tmp_path), (), 1_000_000) == []
        assert old.is_dir()

    def test_an_unused_workspace_past_the_ttl_is_deleted(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_workspaces

        old = self._aged(tmp_path, "old", age=10_000)

        swept = sweep_workspaces(self._config(tmp_path, workspace_ttl=100), (), 1_000_000)

        assert swept == ["old"]
        assert not old.exists()

    def test_a_recent_workspace_is_kept(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_workspaces

        fresh = self._aged(tmp_path, "fresh", age=10)

        assert sweep_workspaces(self._config(tmp_path, workspace_ttl=100), (), 1_000_000) == []
        assert fresh.is_dir()

    def test_an_open_session_is_never_swept(self, tmp_path):
        """It may be older than the TTL precisely because it stayed open."""
        from pydantic_ai_backends.remote.server import sweep_workspaces

        old = self._aged(tmp_path, "busy", age=10_000)

        swept = sweep_workspaces(self._config(tmp_path, workspace_ttl=100), ["busy"], 1_000_000)

        assert swept == []
        assert old.is_dir()

    def test_files_are_ignored(self, tmp_path):
        import os

        from pydantic_ai_backends.remote.server import sweep_workspaces

        stray = tmp_path / "stray.txt"
        stray.write_text("not a workspace")
        os.utime(stray, (0, 0))

        assert sweep_workspaces(self._config(tmp_path, workspace_ttl=1), (), 1_000_000) == []
        assert stray.exists()

    def test_a_missing_root_sweeps_nothing(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_workspaces

        config = self._config(tmp_path / "absent", workspace_ttl=1)

        assert sweep_workspaces(config, (), 1_000_000) == []

    def test_opening_a_session_refreshes_its_directory(self, tmp_path):
        """The sweep ages a directory from when its session was last opened."""
        config = self._config(tmp_path, workspace_ttl=100)
        _session_volumes(config, "s1")
        os.utime(tmp_path / "s1", (0, 0))

        _session_volumes(config, "s1")

        assert (tmp_path / "s1").stat().st_mtime > 0


class TestSweepLoop:
    """The background pass that reclaims workspaces."""

    def test_the_loop_runs_only_when_a_ttl_is_set(self, tmp_path):
        harness = Harness(workspace_root=str(tmp_path))
        with harness.client():
            assert harness.app.state.service._sweep_task is None

        harness = Harness(workspace_root=str(tmp_path), workspace_ttl=60)
        with harness.client():
            task = harness.app.state.service._sweep_task
            assert task is not None
        # Shutdown cancels it, so it cannot outlive the app.
        assert harness.app.state.service._sweep_task is None
        assert task.cancelled() or task.cancelling()

    async def test_a_pass_deletes_what_the_sweep_finds(self, tmp_path):
        harness = Harness(workspace_root=str(tmp_path), workspace_ttl=1, cleanup_interval=0)
        service = harness.app.state.service
        aged = tmp_path / "old"
        (aged / "workspace").mkdir(parents=True)
        os.utime(aged, (0, 0))

        task = asyncio.create_task(service._sweep_loop())
        for _ in range(100):
            await asyncio.sleep(0)
            if not aged.exists():
                break
        # A few more turns, so a pass that finds nothing also runs.
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()

        assert not aged.exists()
        assert not task.done()

    async def test_a_failing_pass_is_logged_and_the_loop_survives(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
    ):
        """A loop that exits leaves every later workspace to accumulate."""
        from pydantic_ai_backends.remote import server as server_mod

        harness = Harness(workspace_root=str(tmp_path), workspace_ttl=1, cleanup_interval=0)
        service = harness.app.state.service
        calls: list[int] = []

        def explode(*args: Any, **kwargs: Any) -> list[str]:
            calls.append(1)
            raise RuntimeError("disk gone")

        monkeypatch.setattr(server_mod, "sweep_workspaces", explode)

        with caplog.at_level("ERROR"):
            task = asyncio.create_task(service._sweep_loop())
            for _ in range(100):
                await asyncio.sleep(0)
                if len(calls) >= 2:
                    break
            task.cancel()

        assert len(calls) >= 2
        assert "Workspace sweep failed" in caplog.text


class TestLazySessions:
    """A sandbox nobody uses must not cost a container."""

    def test_constructing_one_opens_nothing(self, harness: Harness):
        with harness.client() as client:
            RemoteSandbox(token=SERVICE_TOKEN, session_id="unused", client=client)

            assert harness.built == {}

    def test_the_first_operation_opens_the_session(self, harness: Harness):
        with harness.client() as client:
            sandbox = RemoteSandbox(token=SERVICE_TOKEN, session_id="lazy", client=client)

            sandbox.write("/notes.txt", "hello")

            assert harness.built["lazy"].started == 1

    def test_the_session_is_opened_once(self, harness: Harness):
        with harness.client() as client:
            sandbox = RemoteSandbox(token=SERVICE_TOKEN, session_id="lazy", client=client)

            sandbox.write("/a.txt", "1")
            sandbox.write("/b.txt", "2")
            sandbox.read("/a.txt")

            assert harness.built["lazy"].started == 1

    def test_an_explicit_start_still_pre_warms(self, harness: Harness):
        with harness.client() as client:
            sandbox = RemoteSandbox(token=SERVICE_TOKEN, session_id="warm", client=client)

            sandbox.start()

            assert harness.built["warm"].started == 1

    def test_a_probe_does_not_open_a_session(self, harness: Harness):
        """`is_alive` asks about a session; it must not create one."""
        with harness.client() as client:
            sandbox = RemoteSandbox(token=SERVICE_TOKEN, session_id="probed", client=client)

            assert sandbox.is_alive() is False
            assert harness.built == {}

    def test_operations_degrade_when_the_session_cannot_be_opened(self, harness: Harness):
        harness.next_start_error = RuntimeError("no daemon")
        with harness.client() as client:
            sandbox = RemoteSandbox(token=SERVICE_TOKEN, session_id="doomed", client=client)

            assert sandbox.read_bytes("/x") == b""
            assert sandbox.ls_info("/") == []
            assert sandbox.execute("echo hi").exit_code == 1


class TestWorkspaceArchiveRoutes:
    """Listing and reading a session's files with no sandbox running."""

    @pytest.fixture
    def stored(self, tmp_path):
        """A service with a workspace already on disk, and no session open."""
        harness = Harness(workspace_root=str(tmp_path))
        workspace = tmp_path / "cold" / "workspace"
        (workspace / "src").mkdir(parents=True)
        (workspace / "report.md").write_text("# Findings\nline two\nline three\n")
        (workspace / "src" / "app.py").write_text("print('hi')\n")
        return harness, workspace

    def test_listing_needs_no_container(self, stored):
        harness, _ = stored
        with harness.client() as client:
            response = client.post(
                "/workspaces/cold/ls", json={"path": "."}, headers=_service_headers()
            )

            assert response.status_code == 200
            rows = [wire.FileEntry.model_validate(row) for row in response.json()]
            assert [(r.name, r.is_dir) for r in rows] == [("src", True), ("report.md", False)]
            # Nothing was started to answer that.
            assert harness.built == {}

    def test_reading_needs_no_container(self, stored):
        harness, _ = stored
        with harness.client() as client:
            response = client.post(
                "/workspaces/cold/read", json={"path": "report.md"}, headers=_service_headers()
            )

            content = wire.ReadResponse.model_validate(response.json()).content
            assert content.startswith("# Findings")
            assert harness.built == {}

    def test_an_in_container_path_resolves_to_the_same_file(self, stored):
        """A UI hands back exactly what a live listing showed it."""
        harness, _ = stored
        with harness.client() as client:
            response = client.post(
                "/workspaces/cold/read",
                json={"path": "/workspace/src/app.py"},
                headers=_service_headers(),
            )

            assert "print" in wire.ReadResponse.model_validate(response.json()).content

    def test_traversal_is_refused(self, stored):
        harness, _ = stored
        with harness.client() as client:
            response = client.post(
                "/workspaces/cold/read",
                json={"path": "../../../etc/passwd"},
                headers=_service_headers(),
            )

            assert response.status_code == 400
            assert "outside the workspace" in response.text

    def test_a_symlink_out_of_the_workspace_is_refused(self, stored):
        """Untrusted code in the sandbox can plant one; the host must not follow it."""
        harness, workspace = stored
        secret = workspace.parent.parent / "host-secret.txt"
        secret.write_text("do not read me")
        (workspace / "escape.txt").symlink_to(secret)

        with harness.client() as client:
            response = client.post(
                "/workspaces/cold/read",
                json={"path": "escape.txt"},
                headers=_service_headers(),
            )

            assert response.status_code == 400
            listed = client.post(
                "/workspaces/cold/ls", json={"path": "."}, headers=_service_headers()
            ).json()
            assert "escape.txt" not in [row["name"] for row in listed]

    def test_an_absolute_host_path_lands_inside_the_workspace(self, stored):
        """`/etc/passwd` becomes `etc/passwd` under the workspace, so it is absent."""
        harness, _ = stored
        with harness.client() as client:
            response = client.post(
                "/workspaces/cold/read",
                json={"path": "/etc/passwd"},
                headers=_service_headers(),
            )

            assert response.status_code == 404
            assert "etc/passwd" in response.text

    def test_a_missing_file_is_404(self, stored):
        harness, _ = stored
        with harness.client() as client:
            response = client.post(
                "/workspaces/cold/read", json={"path": "nope.md"}, headers=_service_headers()
            )

            assert response.status_code == 404

    def test_a_missing_workspace_is_404(self, stored):
        harness, _ = stored
        with harness.client() as client:
            response = client.post(
                "/workspaces/never/ls", json={"path": "."}, headers=_service_headers()
            )

            assert response.status_code == 404

    def test_listing_a_file_is_404(self, stored):
        harness, _ = stored
        with harness.client() as client:
            response = client.post(
                "/workspaces/cold/ls", json={"path": "report.md"}, headers=_service_headers()
            )

            assert response.status_code == 404

    def test_an_oversized_file_is_refused(self, tmp_path):
        harness = Harness(workspace_root=str(tmp_path), max_read_bytes=16)
        workspace = tmp_path / "big" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "huge.txt").write_text("x" * 100)

        with harness.client() as client:
            response = client.post(
                "/workspaces/big/read", json={"path": "huge.txt"}, headers=_service_headers()
            )

            assert response.status_code == 400
            assert "read limit" in response.text

    def test_without_workspace_root_the_archive_says_so(self, harness: Harness):
        with harness.client() as client:
            response = client.post(
                "/workspaces/any/ls", json={"path": "."}, headers=_service_headers()
            )

            assert response.status_code == 409
            assert "workspace_root" in response.text

    def test_a_session_token_is_not_enough(self, stored):
        """A reaped session has none, and the caller here is an application."""
        harness, _ = stored
        with harness.client() as client:
            _, session_token = _open_session(client, session_id="cold")

            response = client.post(
                "/workspaces/cold/ls",
                json={"path": "."},
                headers={wire.TOKEN_HEADER: session_token},
            )

            assert response.status_code == 401

    def test_a_pagination_footer_reports_what_is_left(self, stored):
        harness, _ = stored
        with harness.client() as client:
            response = client.post(
                "/workspaces/cold/read",
                json={"path": "report.md", "offset": 0, "limit": 1},
                headers=_service_headers(),
            )

            content = wire.ReadResponse.model_validate(response.json()).content
            assert "2 more lines" in content

    def test_reading_past_the_end(self, stored):
        harness, _ = stored
        with harness.client() as client:
            response = client.post(
                "/workspaces/cold/read",
                json={"path": "report.md", "offset": 500},
                headers=_service_headers(),
            )

            assert wire.ReadResponse.model_validate(response.json()).content == "[End of file]"


class TestWorkspaceArchiveClient:
    """The typed client raises, because no model is waiting on it."""

    @pytest.fixture
    def archive(self, tmp_path):
        from pydantic_ai_backends.remote import WorkspaceArchive

        harness = Harness(workspace_root=str(tmp_path))
        workspace = tmp_path / "cold" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "notes.md").write_text("kept\n")
        with harness.client() as client:
            yield WorkspaceArchive(token=SERVICE_TOKEN, client=client)

    def test_ls_returns_file_infos(self, archive):
        assert [row["name"] for row in archive.ls("cold")] == ["notes.md"]

    def test_read_returns_the_content(self, archive):
        assert archive.read("cold", "notes.md") == "kept"

    def test_a_missing_workspace_raises_with_the_status(self, archive):
        from pydantic_ai_backends.remote import WorkspaceArchiveError

        with pytest.raises(WorkspaceArchiveError) as excinfo:
            archive.ls("never")

        assert excinfo.value.status_code == 404

    def test_an_unreachable_service_raises(self):
        from pydantic_ai_backends.remote import WorkspaceArchive, WorkspaceArchiveError

        class Broken:
            def post(self, *args: Any, **kwargs: Any) -> Any:
                raise OSError("connection refused")

        archive = WorkspaceArchive(token="t", client=Broken())

        with pytest.raises(WorkspaceArchiveError, match="Could not reach"):
            archive.read("any", "file.txt")

        assert archive.close() is None

    def test_it_builds_its_own_client_when_not_given_one(self):
        from pydantic_ai_backends.remote import WorkspaceArchive

        archive = WorkspaceArchive("http://sandboxd:8080", token="t")
        try:
            assert archive._owns_client is True
        finally:
            archive.close()


class _RaisingHttp:
    """An HTTP client whose every request fails at the transport."""

    def post(self, *args: Any, **kwargs: Any) -> Any:
        raise OSError("connection reset")


class TestLazySessionInternals:
    """The guard that keeps two threads from opening one session twice."""

    def test_only_one_thread_opens_the_session(self):
        import threading

        sandbox = RemoteSandbox(token="t", client=_RaisingHttp())
        entered = threading.Event()
        proceed = threading.Event()
        opens: list[int] = []

        def slow_open() -> None:
            opens.append(1)
            entered.set()
            proceed.wait(2)
            sandbox._started = True

        sandbox.start = slow_open  # type: ignore[method-assign]

        first = threading.Thread(target=sandbox._ensure_session)
        first.start()
        assert entered.wait(2)

        # The first thread holds the lock inside start(); this one must wait and
        # then find the session already open rather than opening a second.
        second = threading.Thread(target=sandbox._ensure_session)
        second.start()
        proceed.set()
        first.join(2)
        second.join(2)

        assert opens == [1]

    def test_a_transport_failure_mid_operation_degrades(self):
        sandbox = RemoteSandbox(token="t", client=_RaisingHttp())
        sandbox._started = True

        assert sandbox.read_bytes("/x") == b""
        assert sandbox.ls_info("/") == []


class TestSandboxRuntimeEntries:
    """The allowlist entry, which is the only thing a client can name."""

    def test_a_bare_image_string_still_works(self):
        config = SandboxdConfig(token="t", runtimes={"a": "img-a"}, default_runtime="a")

        _, runtime = config.resolve_runtime("a")

        assert runtime.image == "img-a"
        assert runtime.builds is False

    def test_exactly_one_of_image_or_runtime_is_required(self):
        from pydantic_ai_backends.remote.server import SandboxRuntime

        with pytest.raises(ValueError, match="exactly one"):
            SandboxRuntime()
        with pytest.raises(ValueError, match="exactly one"):
            SandboxRuntime(image="img", runtime="python-minimal")

    def test_a_named_built_in_runtime_resolves(self):
        from pydantic_ai_backends.remote.server import SandboxRuntime

        entry = SandboxRuntime(runtime="python-datascience")

        assert entry.builds is True
        assert entry.resolved_runtime().name == "python-datascience"
        assert "pandas" in entry.describes()
        assert "package(s)" in entry.image_label()

    def test_a_ready_made_runtime_config_reports_its_image(self):
        from pydantic_ai_backends.remote.server import SandboxRuntime

        entry = SandboxRuntime(runtime="node-minimal")

        assert entry.image_label() == "node:20-slim"

    def test_a_description_overrides_the_derived_one(self):
        from pydantic_ai_backends.remote.server import SandboxRuntime

        entry = SandboxRuntime(image="img", description="the house image")

        assert entry.describes() == "the house image"

    def test_a_bare_image_describes_itself(self):
        from pydantic_ai_backends.remote.server import SandboxRuntime

        assert SandboxRuntime(image="img-a").describes() == "img-a"

    def test_a_built_runtime_without_a_description_says_what_it_builds(self):
        from pydantic_ai_backends.remote.server import SandboxRuntime
        from pydantic_ai_backends.types import RuntimeConfig

        entry = SandboxRuntime(runtime=RuntimeConfig(name="bare", base_image="python:3.12-slim"))

        assert entry.describes() == "built from python:3.12-slim"


class TestPerRuntimeCeilings:
    """One number for a whole service starves some runtimes and over-commits others."""

    def _config(self, **kwargs):
        from pydantic_ai_backends.remote.server import SandboxRuntime

        return SandboxdConfig(
            token="t",
            runtimes={
                "small": "python:3.12-slim",
                "big": SandboxRuntime(image="python:3.12-slim", mem_limit="8g", cpus=4.0),
            },
            default_runtime="small",
            **kwargs,
        )

    def test_a_runtime_without_ceilings_takes_the_service_defaults(self):
        config = self._config(mem_limit="512m", cpus=1.0, pids_limit=128)

        limits = config.limits_for(config.resolve_runtime("small")[1])

        assert limits == {
            "mem_limit": "512m",
            "cpus": 1.0,
            "cpu_shares": None,
            "pids_limit": 128,
            "network_mode": "none",
        }

    def test_a_runtime_may_raise_above_the_service_default(self):
        """The service value is a default, not a maximum."""
        config = self._config(mem_limit="512m", cpus=1.0)

        limits = config.limits_for(config.resolve_runtime("big")[1])

        assert limits["mem_limit"] == "8g"
        assert limits["cpus"] == 4.0

    def test_an_unset_ceiling_on_both_stays_unlimited(self):
        config = self._config(mem_limit=None, cpus=None, pids_limit=None)

        limits = config.limits_for(config.resolve_runtime("small")[1])

        assert limits["mem_limit"] is None
        assert limits["pids_limit"] is None

    def test_one_runtime_can_be_given_the_network(self):
        from pydantic_ai_backends.remote.server import SandboxRuntime

        config = SandboxdConfig(
            token="t",
            runtimes={
                "offline": "python:3.12-slim",
                "online": SandboxRuntime(image="python:3.12-slim", network_mode="bridge"),
            },
            default_runtime="offline",
        )

        assert config.limits_for(config.resolve_runtime("offline")[1])["network_mode"] == "none"
        assert config.limits_for(config.resolve_runtime("online")[1])["network_mode"] == "bridge"

    def test_the_builder_applies_the_runtime_ceilings(self):
        from pydantic_ai_backends.remote.server import SandboxRuntime, _default_builder

        config = self._config(mem_limit="512m", cpus=1.0)

        sandbox = _default_builder(config)(
            "s1",
            SandboxRuntime(image="python:3.12-slim", mem_limit="8g", cpus=4.0, pids_limit=1024),
        )

        assert sandbox._mem_limit == "8g"
        assert sandbox._cpus == 4.0
        assert sandbox._pids_limit == 1024

    def test_the_builder_forces_the_service_work_dir_on_a_built_runtime(self):
        """The volume mount, the archive and a client's paths must agree on one."""
        from pydantic_ai_backends.remote.server import SandboxRuntime, _default_builder

        config = SandboxdConfig(
            token="t",
            runtimes={"node": SandboxRuntime(runtime="node-react")},
            default_runtime="node",
            work_dir="/srv",
        )

        sandbox = _default_builder(config)("s1", SandboxRuntime(runtime="node-react"))

        # node-react asks for /app; the service's directory wins.
        assert sandbox.runtime.work_dir == "/srv"
        assert sandbox._work_dir == "/srv"


class TestRuntimePolicyReporting:
    """An operator has to be able to read the catalogue off the running service."""

    def test_each_runtime_reports_its_effective_ceilings(self):
        from pydantic_ai_backends.remote.server import SandboxRuntime

        harness = Harness(mem_limit="512m", cpus=1.0)
        harness.config.runtimes["heavy"] = SandboxRuntime(
            runtime="python-datascience", mem_limit="4g", description="crunching"
        )
        with harness.client() as client:
            policy = wire.ServicePolicy.model_validate(
                client.get("/policy", headers=_service_headers()).json()
            )

        by_alias = {row.alias: row for row in policy.runtimes}
        assert by_alias["heavy"].mem_limit == "4g"
        assert by_alias["heavy"].cpus == 1.0  # inherited
        assert by_alias["heavy"].builds is True
        assert by_alias["heavy"].description == "crunching"
        assert by_alias["python"].mem_limit == "512m"
        assert by_alias["python"].builds is False


class TestShippedCatalogues:
    """What an operator gets by default, and what they can opt into."""

    def test_the_default_allowlist_builds_nothing(self):
        """A fresh deployment's first session must not wait on an image build."""
        from pydantic_ai_backends.remote.server import DEFAULT_RUNTIMES

        assert all(not entry.builds for entry in DEFAULT_RUNTIMES.values())

    def test_the_default_allowlist_is_the_config_default(self):
        from pydantic_ai_backends.remote.server import DEFAULT_RUNTIMES

        config = SandboxdConfig(token="t")

        assert set(config.runtimes) == set(DEFAULT_RUNTIMES)
        assert config.default_runtime in config.runtimes

    def test_the_suggested_catalogue_is_usable_as_is(self):
        from pydantic_ai_backends.remote.server import SUGGESTED_RUNTIMES

        config = SandboxdConfig(token="t", runtimes=SUGGESTED_RUNTIMES, default_runtime="python")

        for alias in config.runtimes:
            _, runtime = config.resolve_runtime(alias)
            assert runtime.image_label()
            assert config.limits_for(runtime)["mem_limit"]

    def test_only_the_runtimes_that_need_it_are_given_the_network(self):
        """Network access is the decision most worth being explicit about.

        Pinning the set means adding a networked runtime is a deliberate edit
        here rather than something that slips into the catalogue.
        """
        from pydantic_ai_backends.remote.server import SUGGESTED_RUNTIMES

        online = {
            alias for alias, entry in SUGGESTED_RUNTIMES.items() if entry.network_mode == "bridge"
        }

        # Scraping fetches pages; polyglot exists to install what it is missing.
        assert online == {"python-scraping", "polyglot"}


class TestTmpfsAndCpuShares:
    """Two knobs that only matter on a host small enough to notice."""

    def test_every_sandbox_gets_an_in_memory_tmp_by_default(self):
        """Scratch writes otherwise land in the overlay and grow the disk."""
        from pydantic_ai_backends.remote.server import SandboxRuntime, _default_builder

        config = SandboxdConfig(token="t")
        sandbox = _default_builder(config)("s1", SandboxRuntime(image="python:3.12-slim"))

        assert sandbox._tmpfs == {"/tmp": "size=64m"}

    def test_tmpfs_can_be_turned_off(self):
        from pydantic_ai_backends.remote.server import SandboxRuntime, _default_builder

        config = SandboxdConfig(token="t", tmpfs_size=None)
        sandbox = _default_builder(config)("s1", SandboxRuntime(image="python:3.12-slim"))

        assert sandbox._tmpfs == {}

    def test_the_tmpfs_mount_is_executable(self):
        """Docker mounts a tmpfs noexec, which breaks pip building from source."""
        from pydantic_ai_backends.backends.docker.sandbox import DockerSandbox

        sandbox = DockerSandbox(tmpfs={"/tmp": "size=64m"})

        assert sandbox._run_kwargs()["tmpfs"] == {"/tmp": "size=64m,exec"}

    def test_an_explicit_exec_is_not_repeated(self):
        from pydantic_ai_backends.backends.docker.sandbox import DockerSandbox

        sandbox = DockerSandbox(tmpfs={"/tmp": "size=64m,exec"})

        assert sandbox._run_kwargs()["tmpfs"] == {"/tmp": "size=64m,exec"}

    def test_options_may_be_only_exec(self):
        from pydantic_ai_backends.backends.docker.sandbox import DockerSandbox

        assert DockerSandbox(tmpfs={"/tmp": ""})._run_kwargs()["tmpfs"] == {"/tmp": "exec"}

    def test_cpu_shares_reach_the_container(self):
        from pydantic_ai_backends.backends.docker.sandbox import DockerSandbox

        kwargs = DockerSandbox(cpu_shares=512)._run_kwargs()

        assert kwargs["cpu_shares"] == 512

    def test_a_runtime_may_set_its_own_weight(self):
        from pydantic_ai_backends.remote.server import SandboxRuntime

        config = SandboxdConfig(
            token="t",
            runtimes={
                "light": SandboxRuntime(image="python:3.12-slim", cpu_shares=256),
                "heavy": "python:3.12-slim",
            },
            default_runtime="light",
            cpu_shares=1024,
        )

        assert config.limits_for(config.resolve_runtime("light")[1])["cpu_shares"] == 256
        assert config.limits_for(config.resolve_runtime("heavy")[1])["cpu_shares"] == 1024

    def test_shares_are_visible_in_the_policy(self):
        harness = Harness(cpu_shares=512, tmpfs_size="128m")
        with harness.client() as client:
            policy = wire.ServicePolicy.model_validate(
                client.get("/policy", headers=_service_headers()).json()
            )

        assert policy.cpu_shares == 512
        assert policy.tmpfs_size == "128m"
        assert all(row.cpu_shares == 512 for row in policy.runtimes)


class TestPrewarming:
    """The first session on a built runtime should not pay for the build."""

    async def test_the_allowlist_is_warmed_at_startup(self):
        warmed: list[int] = []
        harness = Harness()
        service = harness.app.state.service
        service._prewarm = lambda: warmed.append(1)

        with harness.client():
            for _ in range(100):
                await asyncio.sleep(0)
                if warmed:
                    break

        assert warmed == [1]

    async def test_it_can_be_turned_off(self):
        warmed: list[int] = []
        harness = Harness(prewarm=False)
        service = harness.app.state.service
        service._prewarm = lambda: warmed.append(1)

        with harness.client():
            for _ in range(20):
                await asyncio.sleep(0)

        assert warmed == []
        assert service._prewarm_task is None

    async def test_a_failure_does_not_take_the_service_down(self, caplog):
        def explode() -> None:
            raise RuntimeError("no daemon")

        harness = Harness()
        harness.app.state.service._prewarm = explode

        with caplog.at_level("ERROR"), harness.client() as client:
            for _ in range(100):
                await asyncio.sleep(0)
                if "Prewarming" in caplog.text:
                    break
            assert client.get("/healthz").status_code == 200

        assert "Prewarming the runtime allowlist failed" in caplog.text

    def test_an_injected_builder_gets_no_prewarm(self):
        """Nothing else knows how to warm a builder the service did not make."""
        assert Harness().app.state.service._prewarm is None

    def test_the_default_path_does_get_one(self):
        from pydantic_ai_backends.remote.server import create_app

        app = create_app(SandboxdConfig(token="t"))

        assert app.state.service._prewarm is not None

    def test_the_routine_pulls_ready_images_and_builds_the_rest(self, monkeypatch):
        from pydantic_ai_backends.remote import server as server_mod
        from pydantic_ai_backends.remote.server import SandboxRuntime, _default_prewarm

        pulled: list[str] = []
        built: list[str] = []

        module = types.ModuleType("pydantic_ai_backends.backends.docker._client")
        module.docker_client = lambda: object()
        monkeypatch.setitem(sys.modules, "pydantic_ai_backends.backends.docker._client", module)

        image_module = types.ModuleType("pydantic_ai_backends.backends.docker._image")
        image_module.pull_if_absent = lambda _client, image: bool(pulled.append(image)) or True
        image_module.resolve_image = lambda _client, runtime, _fallback: built.append(runtime.name)
        monkeypatch.setitem(
            sys.modules, "pydantic_ai_backends.backends.docker._image", image_module
        )

        config = SandboxdConfig(
            token="t",
            runtimes={
                "ready": "python:3.12-slim",
                "builds": SandboxRuntime(runtime="python-datascience"),
            },
            default_runtime="ready",
            work_dir="/srv",
        )

        _default_prewarm(config)()

        assert pulled == ["python:3.12-slim"]
        assert built == ["python-datascience"]
        assert server_mod is not None

    def test_one_bad_runtime_does_not_stop_the_others(self, monkeypatch, caplog):
        from pydantic_ai_backends.remote.server import _default_prewarm

        pulled: list[str] = []

        client_module = types.ModuleType("pydantic_ai_backends.backends.docker._client")
        client_module.docker_client = lambda: object()
        monkeypatch.setitem(
            sys.modules, "pydantic_ai_backends.backends.docker._client", client_module
        )

        def pull(_client, image):
            if "broken" in image:
                raise RuntimeError("manifest unknown")
            pulled.append(image)
            return True

        image_module = types.ModuleType("pydantic_ai_backends.backends.docker._image")
        image_module.pull_if_absent = pull
        image_module.resolve_image = lambda *a: None
        monkeypatch.setitem(
            sys.modules, "pydantic_ai_backends.backends.docker._image", image_module
        )

        config = SandboxdConfig(
            token="t",
            runtimes={"a-broken": "broken:1", "z-fine": "python:3.12-slim"},
            default_runtime="z-fine",
        )

        with caplog.at_level("ERROR"):
            _default_prewarm(config)()

        assert pulled == ["python:3.12-slim"]
        assert "Could not prepare runtime a-broken" in caplog.text


class TestUsageSamplingScales:
    """One stats call costs over a second, so N of them cannot be sequential."""

    async def test_samples_are_taken_concurrently(self):
        """Sequentially, a dashboard poll took a second per session."""
        harness = Harness()
        with harness.client() as client:
            for index in range(6):
                _open_session(client, session_id=f"s-{index}")

            for sandbox in harness.built.values():
                sandbox.usage = SandboxUsage(memory_bytes=1024)
                sandbox.sample_delay = 0.05

            service = harness.app.state.service
            started = time.monotonic()
            listing = await service.listing(usage=True)
            elapsed = time.monotonic() - started

        assert len(listing.sessions) == 6
        # Six 50 ms samples: concurrent finishes well inside the 300 ms a
        # sequential pass would need.
        assert elapsed < 0.2

    async def test_a_sample_is_reused_within_the_cache_window(self):
        harness = Harness()
        with harness.client() as client:
            _open_session(client, session_id="cached")
            sandbox = harness.built["cached"]
            sandbox.usage = SandboxUsage(memory_bytes=2048)
            service = harness.app.state.service

            await service.listing(usage=True)
            await service.listing(usage=True)
            await service.listing(usage=True)

        assert sandbox.usage_calls == 1

    async def test_the_cache_expires(self, monkeypatch):
        from pydantic_ai_backends.remote import server as server_mod

        harness = Harness()
        with harness.client() as client:
            _open_session(client, session_id="expiring")
            sandbox = harness.built["expiring"]
            sandbox.usage = SandboxUsage(memory_bytes=2048)
            service = harness.app.state.service

            clock = [1000.0]
            monkeypatch.setattr(server_mod.time, "monotonic", lambda: clock[0])
            await service.listing(usage=True)
            clock[0] += server_mod.USAGE_CACHE_SECONDS + 0.1
            await service.listing(usage=True)

        assert sandbox.usage_calls == 2

    async def test_nothing_is_sampled_when_usage_is_not_asked_for(self):
        harness = Harness()
        with harness.client() as client:
            _open_session(client, session_id="unsampled")
            sandbox = harness.built["unsampled"]
            sandbox.usage = SandboxUsage(memory_bytes=2048)

            listing = await harness.app.state.service.listing(usage=False)

        assert sandbox.usage_calls == 0
        assert listing.sessions[0].usage is None

    async def test_a_released_session_drops_its_cached_sample(self):
        harness = Harness()
        with harness.client() as client:
            _open_session(client, session_id="dropped")
            harness.built["dropped"].usage = SandboxUsage(memory_bytes=1)
            service = harness.app.state.service
            await service.listing(usage=True)

            await service.close_session("dropped")

        assert "dropped" not in service._usage_cache


class TestUncappedSessions:
    """`max_sessions=None` is for hosts where something else does the bounding."""

    def test_the_ceiling_can_be_removed(self):
        config = SandboxdConfig(token="t", max_sessions=None)

        assert config.max_sessions is None

    def test_an_uncapped_service_reports_it(self):
        harness = Harness(max_sessions=None)
        with harness.client() as client:
            listing = wire.SessionList.model_validate(
                client.get("/sessions", headers=_service_headers()).json()
            )

        assert listing.limit is None

    async def test_nothing_is_evicted_without_a_ceiling(self, tmp_path):
        harness = Harness(max_sessions=None, evict_idle_after=0, workspace_root=str(tmp_path))
        with harness.client():
            assert await harness.app.state.service.make_room() is None


class TestEviction:
    """Turning the session ceiling into a working-set size."""

    def _harness(self, tmp_path, **kwargs) -> Harness:
        return Harness(workspace_root=str(tmp_path), **kwargs)

    def test_eviction_needs_a_workspace_to_come_back_to(self):
        with pytest.raises(ValueError, match="evict_idle_after needs workspace_root"):
            SandboxdConfig(token="t", evict_idle_after=60)

    async def test_the_least_recently_used_idle_session_makes_room(self, tmp_path):
        harness = self._harness(tmp_path, max_sessions=2, evict_idle_after=0)
        with harness.client() as client:
            _open_session(client, session_id="oldest")
            _open_session(client, session_id="newest")
            harness.built["oldest"]._last_activity = time.time() - 500
            harness.built["newest"]._last_activity = time.time()

            response = client.post(
                "/sessions", json={"session_id": "third"}, headers=_service_headers()
            )

            assert response.status_code == 200
            service = harness.app.state.service
            assert "oldest" not in service._sessions
            assert "newest" in service._sessions
            assert "third" in service._sessions

    async def test_an_evicted_session_keeps_its_workspace(self, tmp_path):
        """Which is what makes eviction cheap rather than destructive."""
        harness = self._harness(tmp_path, max_sessions=1, evict_idle_after=0)
        with harness.client() as client:
            _open_session(client, session_id="kept")
            harness.built["kept"]._last_activity = time.time() - 500

            client.post("/sessions", json={"session_id": "next"}, headers=_service_headers())

        assert (tmp_path / "kept" / "workspace").is_dir()

    async def test_a_busy_session_is_not_evicted(self, tmp_path):
        """Killing an agent's work to serve someone's first request is worse."""
        harness = self._harness(tmp_path, max_sessions=1, evict_idle_after=60)
        with harness.client() as client:
            _open_session(client, session_id="busy")
            harness.built["busy"]._last_activity = time.time()

            response = client.post(
                "/sessions", json={"session_id": "next"}, headers=_service_headers()
            )

            assert response.status_code == 429
            assert "busy" in harness.app.state.service._sessions

    async def test_without_the_setting_the_cap_still_refuses(self, tmp_path):
        harness = self._harness(tmp_path, max_sessions=1)
        with harness.client() as client:
            _open_session(client, session_id="only")
            harness.built["only"]._last_activity = time.time() - 5000

            response = client.post(
                "/sessions", json={"session_id": "next"}, headers=_service_headers()
            )

            assert response.status_code == 429

    async def test_room_is_only_made_when_the_pool_is_full(self, tmp_path):
        harness = self._harness(tmp_path, max_sessions=5, evict_idle_after=0)
        with harness.client() as client:
            _open_session(client, session_id="alone")
            harness.built["alone"]._last_activity = time.time() - 500

            assert await harness.app.state.service.make_room() is None
            assert "alone" in harness.app.state.service._sessions

    def test_the_setting_is_visible_in_the_policy(self, tmp_path):
        harness = self._harness(tmp_path, evict_idle_after=45)
        with harness.client() as client:
            policy = wire.ServicePolicy.model_validate(
                client.get("/policy", headers=_service_headers()).json()
            )

        assert policy.evict_idle_after == 45


class TestEvictionNeverInterruptsWork:
    """`last_activity` is stamped when a command starts, so it is not enough."""

    async def test_a_session_running_a_command_is_not_evicted(self, tmp_path):
        harness = Harness(max_sessions=1, evict_idle_after=0, workspace_root=str(tmp_path))
        with harness.client() as client:
            _open_session(client, session_id="working")
            service = harness.app.state.service
            # A command that began a while ago and is still going: idle by the
            # timestamp, plainly busy in reality.
            harness.built["working"]._last_activity = time.time() - 600

            with service.observe("working", "exec", "sleep 600"):
                assert await service.make_room() is None

            assert "working" in service._sessions

    async def test_the_same_session_becomes_evictable_once_it_finishes(self, tmp_path):
        harness = Harness(max_sessions=1, evict_idle_after=0, workspace_root=str(tmp_path))
        with harness.client() as client:
            _open_session(client, session_id="finished")
            service = harness.app.state.service
            harness.built["finished"]._last_activity = time.time() - 600

            with service.observe("finished", "exec", "true"):
                pass

            assert await service.make_room() == "finished"

    async def test_nested_operations_are_counted(self, tmp_path):
        harness = Harness(max_sessions=1, evict_idle_after=0, workspace_root=str(tmp_path))
        with harness.client() as client:
            _open_session(client, session_id="busy")
            service = harness.app.state.service
            harness.built["busy"]._last_activity = time.time() - 600

            with service.observe("busy", "read", "/a"):
                with service.observe("busy", "read", "/b"):
                    pass
                # The outer one is still in flight.
                assert await service.make_room() is None

            assert await service.make_room() == "busy"


class _FakeContainer:
    def __init__(self, name: str, finished: str, running: bool = False):
        self.name = name
        self.attrs = {"State": {"Running": running, "FinishedAt": finished}}
        self.removed = False

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _FakeContainers:
    def __init__(self, containers: list[_FakeContainer]):
        self._containers = containers
        self.filters: dict | None = None

    def list(self, all: bool = False, filters: dict | None = None):
        self.filters = filters
        return self._containers


class _FakeDocker:
    def __init__(self, containers: list[_FakeContainer]):
        self.containers = _FakeContainers(containers)


class TestContainerSweep:
    """Reclaiming what a session built, while its files stay put.

    The clock passed in is 2027, comfortably after the stop times the fakes
    report — a "now" before them would make every container read as not yet old.
    """

    def _config(self, tmp_path, **kwargs):
        return SandboxdConfig(
            token="t",
            workspace_root=str(tmp_path),
            persist_containers=True,
            **kwargs,
        )

    def test_a_long_stopped_container_is_removed(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_containers

        old = _FakeContainer("sandboxd-old", "2026-06-01T10:00:00.000000000Z")
        client = _FakeDocker([old])

        removed = sweep_containers(
            self._config(tmp_path, container_ttl=3600), client, 1_800_000_000.0
        )

        assert removed == ["sandboxd-old"]
        assert old.removed is True

    def test_the_workspace_is_not_touched(self, tmp_path):
        """The whole point: the build goes, the files stay."""
        from pydantic_ai_backends.remote.server import sweep_containers

        workspace = tmp_path / "old" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "notes.md").write_text("kept")
        client = _FakeDocker([_FakeContainer("sandboxd-old", "2026-06-01T10:00:00Z")])

        sweep_containers(self._config(tmp_path, container_ttl=3600), client, 1_800_000_000.0)

        assert (workspace / "notes.md").read_text() == "kept"

    def test_a_running_container_is_left_alone(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_containers

        live = _FakeContainer("sandboxd-live", "0001-01-01T00:00:00Z", running=True)

        removed = sweep_containers(
            self._config(tmp_path, container_ttl=1), _FakeDocker([live]), 1_800_000_000.0
        )

        assert removed == []
        assert live.removed is False

    def test_a_recently_stopped_container_is_kept(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_containers

        recent = _FakeContainer("sandboxd-recent", "2026-07-31T10:00:00Z")
        stopped_at = datetime.fromisoformat("2026-07-31T10:00:00+00:00").timestamp()

        removed = sweep_containers(
            self._config(tmp_path, container_ttl=86400), _FakeDocker([recent]), stopped_at + 60
        )

        assert removed == []

    def test_a_container_that_never_ran_is_skipped(self, tmp_path):
        """Docker writes a zero timestamp, which must not read as ancient."""
        from pydantic_ai_backends.remote.server import sweep_containers

        never = _FakeContainer("sandboxd-never", "0001-01-01T00:00:00Z")

        assert (
            sweep_containers(
                self._config(tmp_path, container_ttl=1), _FakeDocker([never]), 1_800_000_000.0
            )
            == []
        )

    def test_an_unparseable_timestamp_is_skipped(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_containers

        odd = _FakeContainer("sandboxd-odd", "not a timestamp")

        assert (
            sweep_containers(
                self._config(tmp_path, container_ttl=1), _FakeDocker([odd]), 1_800_000_000.0
            )
            == []
        )

    def test_a_missing_state_is_skipped(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_containers

        blank = _FakeContainer("sandboxd-blank", "2026-01-01T00:00:00Z")
        blank.attrs = {}

        assert (
            sweep_containers(
                self._config(tmp_path, container_ttl=1), _FakeDocker([blank]), 1_800_000_000.0
            )
            == []
        )

    def test_nothing_is_swept_without_a_ttl(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_containers

        old = _FakeContainer("sandboxd-old", "2026-01-01T00:00:00Z")

        assert sweep_containers(self._config(tmp_path), _FakeDocker([old]), 1_800_000_000.0) == []

    def test_nothing_is_swept_when_containers_are_not_persisted(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_containers

        config = SandboxdConfig(token="t", workspace_root=str(tmp_path), container_ttl=1)
        old = _FakeContainer("sandboxd-old", "2026-01-01T00:00:00Z")

        assert sweep_containers(config, _FakeDocker([old]), 1_800_000_000.0) == []

    def test_only_sandbox_containers_are_considered(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_containers

        client = _FakeDocker([])
        sweep_containers(self._config(tmp_path, container_ttl=1), client, 1_800_000_000.0)

        assert client.containers.filters == {"name": "sandboxd-"}

    def test_a_removal_failure_does_not_stop_the_sweep(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_containers

        class Stubborn(_FakeContainer):
            def remove(self, force: bool = False) -> None:
                raise RuntimeError("removal in progress")

        stubborn = Stubborn("sandboxd-a", "2026-01-01T00:00:00Z")
        fine = _FakeContainer("sandboxd-b", "2026-01-01T00:00:00Z")

        removed = sweep_containers(
            self._config(tmp_path, container_ttl=1), _FakeDocker([stubborn, fine]), 1_800_000_000.0
        )

        assert removed == ["sandboxd-b"]

    def test_a_listing_failure_does_not_break_the_loop(self, tmp_path):
        from pydantic_ai_backends.remote.server import sweep_containers

        class Exploding:
            @property
            def containers(self):
                raise RuntimeError("daemon gone")

        config = self._config(tmp_path, container_ttl=1)

        assert sweep_containers(config, Exploding(), 1_800_000_000.0) == []

    def test_the_ttl_is_visible_in_the_policy(self, tmp_path):
        harness = Harness(
            workspace_root=str(tmp_path), persist_containers=True, container_ttl=2_592_000
        )
        with harness.client() as client:
            policy = wire.ServicePolicy.model_validate(
                client.get("/policy", headers=_service_headers()).json()
            )

        assert policy.container_ttl == 2_592_000

    async def test_the_sweep_loop_runs_for_containers_alone(self, tmp_path):
        """A deployment keeping files for ever still wants builds reclaimed."""
        harness = Harness(
            workspace_root=str(tmp_path),
            persist_containers=True,
            container_ttl=1,
            workspace_ttl=None,
        )
        with harness.client():
            assert harness.app.state.service._sweep_task is not None
