"""Opt-in persistent Browser/Home acceptance, using only disposable fixture resources."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import inspect
import json
import secrets
import shlex
import signal
import socket
import subprocess
import sys
from contextlib import closing, suppress
from pathlib import Path
from uuid import uuid4

import docker
import httpx
from docker.models.containers import Container
from docker.models.networks import Network
from proxy_acceptance import wait_ready
from pydantic_ai.workspaces import Workspace, WorkspaceRef

from pydantic_ai_backends.browser import (
    BrowserError,
    BrowserTab,
    ManagedBrowserConnection,
    SandboxdBrowser,
)
from pydantic_ai_backends.desktop import Key, Launch, TypeText, open_sandbox_desktop
from pydantic_ai_backends.remote.wire import SessionCreated
from pydantic_ai_backends.workspaces import SandboxdWorkspace

ORIGIN = "https://fixture.internal:4443/"


def result_value(result: dict[str, object]) -> object:
    value = result.get("result")
    assert isinstance(value, dict), "CDP evaluation returned no result"
    return value.get("value")


async def evaluate_async(tab: BrowserTab, expression: str) -> object:
    """Wait for fixture promises without changing the SDK's synchronous evaluate contract."""
    await tab.evaluate(
        "window.qaAsyncResult=undefined;(async()=>{try{window.qaAsyncResult={value:await ("
        + expression
        + ")};}catch(error){window.qaAsyncResult={error:error.name};}})();true"
    )
    for _ in range(50):
        found = result_value(await tab.evaluate("window.qaAsyncResult"))
        if isinstance(found, dict):
            assert "error" not in found, "Asynchronous fixture operation failed"
            return found.get("value")
        await asyncio.sleep(0.1)
    raise AssertionError("Asynchronous fixture operation did not finish")


def new_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


class Acceptance:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.folder: Path = args.folder
        self.prefix = "qa-profile-" + uuid4().hex[:10]
        self.sessions: list[str] = []
        self.connections: list[ManagedBrowserConnection] = []
        self.network: Network | None = None
        self.fixture: Container | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.token = ""
        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            self.port = port.getsockname()[1]
        self.base = "http://127.0.0.1:" + str(self.port)
        self.checks: list[str] = []
        self.report: dict[str, object] = {
            "browser_only": args.browser_only,
            "sandbox_idle_seconds": args.sandbox_idle_seconds,
            "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
            "scope": "Real local HTTPS fixture; persistent login, localStorage, IndexedDB and HOME",
            "model_calls": 0,
            "checks": self.checks,
            "fixture_proxy": "Only CONNECT fixture.internal:4443; no outbound network",
            "certificate_policy": "Test-only certificate bypass on isolated disposable Browser",
        }

    async def setup(self) -> None:
        if "profile_key" not in inspect.signature(SandboxdBrowser.start).parameters:
            raise RuntimeError("Install the persistent-profile candidate wheel before running")
        self.folder.mkdir(parents=True, exist_ok=False, mode=0o700)
        tls = self.folder / "fixture-tls"
        tls.mkdir(mode=0o755)
        certificate, key = tls / "fixture.crt", tls / "fixture.key"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=fixture.internal",
                "-addext",
                "subjectAltName=DNS:fixture.internal",
                "-keyout",
                str(key),
                "-out",
                str(certificate),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # The enclosing folder is private; the unprivileged fixture receives this read-only mount.
        key.chmod(0o644)
        with closing(docker.from_env()) as client:
            self.network = client.networks.create(self.prefix, driver="bridge", internal=True)
            self.fixture = client.containers.run(
                self.args.browser_image,
                [
                    "python3",
                    "/fixture/server.py",
                    "--certificate",
                    "/fixture/tls/fixture.crt",
                    "--private-key",
                    "/fixture/tls/fixture.key",
                ],
                detach=True,
                name=self.prefix + "-https",
                network=self.prefix,
                volumes={
                    str(Path(__file__).with_name("persistence_fixture.py").resolve()): {
                        "bind": "/fixture/server.py",
                        "mode": "ro",
                    },
                    str(tls.resolve()): {"bind": "/fixture/tls", "mode": "ro"},
                },
                mem_limit="128m",
                nano_cpus=500_000_000,
                pids_limit=64,
            )
        with (self.folder / "server.log").open("wb") as log:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).with_name("server.py")),
                "--folder",
                str(self.folder / "service"),
                "--port",
                str(self.port),
                "--browser-image",
                self.args.browser_image,
                "--desktop-image",
                self.args.desktop_image,
                "--browser-network",
                self.prefix,
                "--browser-proxy",
                "http://" + self.prefix + "-https:3128",
                "--debug-task-stacks",
                "--persist-browser-profiles",
                "--persist-desktop-home",
                "--sandbox-idle-seconds",
                str(self.args.sandbox_idle_seconds),
                stdout=log,
                stderr=log,
            )
        async with httpx.AsyncClient(base_url=self.base) as client:
            await wait_ready(client)
        self.token = (self.folder / "service" / "service-token").read_text()

    async def owner_lock(self) -> None:
        """A second supervised owner must fail before attaching any runtime."""
        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            second_port = port.getsockname()[1]
        other = self.folder / "second-owner"
        with (self.folder / "second-owner.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).with_name("server.py")),
                "--folder",
                str(other),
                "--port",
                str(second_port),
                "--workspace-root",
                str(self.folder / "service" / "workspaces"),
                "--browser-image",
                self.args.browser_image,
                "--desktop-image",
                self.args.desktop_image,
                "--persist-browser-profiles",
                stdout=log,
                stderr=log,
            )
        try:
            code = await asyncio.wait_for(process.wait(), timeout=8)
            assert code != 0, "A second persistent owner acquired the same workspace root"
            log_text = (self.folder / "second-owner.log").read_text()
            assert "owner" in log_text.lower() or "lock" in log_text.lower(), (
                "The second service failed for an unrelated reason"
            )
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            credential = other / "service-token"
            if credential.exists():
                credential.unlink()
        self.checks.append(
            "Second persistent service owner refuses the same storage root at startup"
        )

    async def create(self, suffix: str, runtime: str = "browser") -> SessionCreated:
        sid = self.prefix + "-" + suffix
        self.sessions.append(sid)
        async with httpx.AsyncClient(base_url=self.base, timeout=60) as client:
            response = await client.post(
                "/sessions",
                headers={"X-Sandbox-Token": self.token},
                json={"session_id": sid, "runtime": runtime, "tenant": self.prefix + "-" + suffix},
            )
            response.raise_for_status()
            return SessionCreated.model_validate_json(response.content)

    async def page(self, api: SandboxdBrowser) -> tuple[ManagedBrowserConnection, BrowserTab]:
        connection = await api.connect()
        self.connections.append(connection)
        # Only this disposable Browser can reach the test origin; no real credentials are used.
        await connection.transport.call("Security.setIgnoreCertificateErrors", {"ignore": True})
        tab = await connection.browser.new_tab()
        await tab.navigate(ORIGIN)
        for _ in range(50):
            if result_value(await tab.evaluate("window.fixtureReady === true")) is True:
                return connection, tab
            await asyncio.sleep(0.1)
        raise AssertionError("HTTPS fixture did not become ready")

    def remove_own_container(self, sid: str) -> None:
        if sid not in self.sessions:
            raise RuntimeError("Refusing to remove a foreign session")
        with closing(docker.from_env()) as client:
            container = client.containers.get("sandboxd-" + sid)
            if container.status == "running":
                container.reload()
            assert container.status != "running", "Suspend must stop the original container"
            container.remove()

    async def browser_roundtrip(self) -> None:
        session = await self.create("browser-a")
        sid = session.session.session_id
        api = SandboxdBrowser(self.base, sid, session.token, timeout=40)
        key, marker = new_key(), secrets.token_hex(16)
        initial = await api.start(profile_key=key)
        assert initial.persistent is True and initial.profile_restored is False
        connection, tab = await self.page(api)
        old_context, old_target = connection.browser.context_id, tab.id
        assert (
            await evaluate_async(
                tab,
                "(async()=>{await fetch('/login',{method:'POST'}); "
                "return await fixturePut(" + json.dumps(marker) + ");})()",
            )
            is True
        )
        before = await evaluate_async(tab, "fixtureRead()")
        assert isinstance(before, dict)
        assert before["authenticated"] is True and before["secureContext"] is True
        assert before["persistentAuthenticated"] is True and before["sessionAuthenticated"] is True
        assert before["cookieVisibleToJS"] == "", "Auth cookies must be HttpOnly"
        self.checks.append(
            "Local HTTPS fixture validates persistent and session-only HttpOnly Secure cookies; "
            "localStorage and IndexedDB transaction commit in a secure context"
        )
        await connection.aclose()
        await api.suspend()
        self.remove_own_container(sid)
        resumed = await api.start(profile_key=key)
        assert resumed.persistent is True and resumed.profile_restored is True
        assert resumed.generation != initial.generation
        try:
            await api.connect(context_id=old_context)
        except BrowserError:
            pass
        else:
            raise AssertionError("A retired live context was reused after container recreation")
        fresh, restored = await self.page(api)
        assert fresh.browser.context_id != old_context
        try:
            await fresh.browser.tab(old_target)
        except BrowserError:
            pass
        else:
            raise AssertionError("A retired target identity was reused after container recreation")
        after = await evaluate_async(restored, "fixtureRead()")
        assert after == before
        self.checks.append(
            "Persistent and session-only Secure HttpOnly login cookies, localStorage and "
            "committed IndexedDB survive "
            "suspend and forced container recreation; live generation identity changes"
        )
        await evaluate_async(restored, "fetch('/logout',{method:'POST'}).then(()=>true)")
        await fresh.aclose()
        await api.suspend()
        self.remove_own_container(sid)
        await api.start(profile_key=key)
        logged_out, page = await self.page(api)
        state = await evaluate_async(page, "fixtureRead()")
        assert isinstance(state, dict) and state["authenticated"] is False
        assert state["indexedDB"] == marker and state["local"] == marker
        await logged_out.aclose()
        await api.end()
        encrypted = self.folder / "service" / "workspaces" / sid / "private" / "browser-profile.enc"
        assert encrypted.is_file()
        self.remove_own_container(sid)
        await api.start(profile_key=key)
        after_end, end_page = await self.page(api)
        assert await evaluate_async(end_page, "fixtureRead()") == state
        await after_end.aclose()
        await api.suspend()
        self.checks.append(
            "Logout remains effective after resume; ordinary End retains saved profile"
        )
        await self.profile_failures(api, key, sid)
        await self.scope_isolation(api, key, sid)
        await api.purge()
        assert not (
            self.folder / "service" / "workspaces" / sid / "private" / "browser-profile.enc"
        ).exists()
        self.checks.append("Explicit profile purge succeeds after suspend")

    async def profile_failures(self, api: SandboxdBrowser, key: str, sid: str) -> None:
        encrypted = self.folder / "service" / "workspaces" / sid / "private" / "browser-profile.enc"
        payload = encrypted.read_bytes()
        assert payload
        assert key.encode() not in payload, (
            "The profile encryption key must not be stored in its blob"
        )
        for bad_key in [new_key()]:
            try:
                await api.start(profile_key=bad_key)
            except BrowserError:
                pass
            else:
                raise AssertionError("Wrong profile key was accepted")
        assert encrypted.read_bytes() == payload
        self.checks.append("Wrong profile key refuses resume and leaves encrypted state unchanged")
        encrypted.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
        try:
            await api.start(profile_key=key)
        except BrowserError:
            pass
        else:
            raise AssertionError("Corrupt profile state was accepted")
        finally:
            encrypted.write_bytes(payload)
        self.checks.append(
            "Corrupt encrypted state refuses resume without silent fresh-profile fallback"
        )

    async def scope_isolation(self, api: SandboxdBrowser, key: str, sid: str) -> None:
        first, second = await asyncio.gather(api.start(profile_key=key), api.start(profile_key=key))
        assert first.created_at == second.created_at
        a, b = await asyncio.gather(api.connect(), api.connect())
        self.connections.extend([a, b])
        assert a.browser.context_id == b.browser.context_id
        await a.aclose()
        await b.aclose()
        await api.suspend()
        self.checks.append("Concurrent starts/viewers share one live profile writer/generation")
        other = await self.create("browser-b")
        foreign = SandboxdBrowser(self.base, other.session.session_id, other.token)
        await foreign.start(profile_key=key)
        connection, page = await self.page(foreign)
        state = await evaluate_async(page, "fixtureRead()")
        assert isinstance(state, dict)
        assert state["authenticated"] is False and state["local"] is None
        assert state["indexedDB"] is None
        try:
            await SandboxdBrowser(self.base, sid, other.token).inspect()
        except BrowserError:
            pass
        else:
            raise AssertionError("Another session's token accessed a persisted-profile owner")
        async with httpx.AsyncClient(base_url=self.base, timeout=30) as client:
            rebound = await client.post(
                "/sessions",
                headers={"X-Sandbox-Token": self.token},
                json={
                    "session_id": sid,
                    "runtime": "browser",
                    "tenant": self.prefix + "-foreign",
                    "reuse": True,
                },
            )
            assert rebound.status_code in (400, 403, 409), "Tenant ownership was rebound"
        await connection.aclose()
        await foreign.purge()
        self.checks.append(
            "Different tenant/session has no auth/localStorage/IndexedDB even with the same key; "
            "cross-session token and tenant rebinding are refused"
        )

    async def desktop_home(self) -> None:
        session = await self.create("desktop", runtime="desktop")
        sid = session.session.session_id
        route = "/sessions/" + sid + "/environments/desktop"
        headers = {"X-Sandbox-Token": session.token}
        async with httpx.AsyncClient(base_url=self.base, timeout=60) as client:
            response = await client.post(
                route,
                headers=headers,
                json={"width": 1024, "height": 768, "idle_seconds": 3, "lifetime_seconds": 60},
            )
            response.raise_for_status()
        capability = SandboxdWorkspace(
            service_url=self.base,
            token=self.token,
            runtime="desktop",
            tenant=self.prefix + "-desktop",
        )
        workspace = Workspace(capability.backend(WorkspaceRef(provider="sandboxd", id=sid)))
        desktop = await open_sandbox_desktop(workspace, width=1024, height=768)
        terminal = next(app for app in await desktop.apps() if app.id == "xfce4-terminal")
        await desktop.perform(Launch(app=terminal.id))
        marker = secrets.token_hex(16)
        command = (
            'mkdir -p "$HOME/.config/persistence-acceptance"; '
            "printf %s " + shlex.quote(marker) + ' > "$HOME/saved-acceptance.txt"; '
            "printf %s "
            + shlex.quote(marker)
            + ' > "$HOME/.config/persistence-acceptance/setting"; '
            "printf %s " + shlex.quote(marker) + " > /workspace/saved-acceptance.txt"
        )
        await desktop.perform(TypeText(text=command))
        await desktop.perform(Key(keys="Return"))
        await asyncio.sleep(0.4)
        assert (await workspace.run(["cat", "/workspace/saved-acceptance.txt"])).stdout == marker
        with closing(docker.from_env()) as client:
            old = client.containers.get("sandboxd-" + sid)
            old_id = old.id
            for _ in range(100):
                old.reload()
                if old.status != "running":
                    break
                await asyncio.sleep(0.1)
            assert old.status != "running", "Desktop idle did not stop its exact container"
        self.remove_own_container(sid)
        # Plain idle may retire Desktop auth; only the trusted application can reopen files.
        async with httpx.AsyncClient(base_url=self.base, timeout=60) as client:
            opened = await client.post(
                "/sessions",
                headers={"X-Sandbox-Token": self.token},
                json={
                    "session_id": sid,
                    "runtime": "desktop",
                    "attach": True,
                    "tenant": self.prefix + "-desktop",
                },
            )
            opened.raise_for_status()
            fresh_session = SessionCreated.model_validate_json(opened.content)
            headers = {"X-Sandbox-Token": fresh_session.token}
            resumed = await client.post(
                route,
                headers=headers,
                json={"width": 1024, "height": 768, "idle_seconds": 30, "lifetime_seconds": 60},
            )
            resumed.raise_for_status()
        with closing(docker.from_env()) as client:
            assert client.containers.get("sandboxd-" + sid).id != old_id
        fresh = Workspace(capability.backend(WorkspaceRef(provider="sandboxd", id=sid)))
        restored = await open_sandbox_desktop(fresh, width=1024, height=768)
        result = await fresh.run(
            [
                "sh",
                "-c",
                'cat "$HOME/saved-acceptance.txt"; '
                'cat "$HOME/.config/persistence-acceptance/setting"; '
                "cat /workspace/saved-acceptance.txt",
            ]
        )
        assert result.stdout == marker * 3
        await restored.aclose()
        self.checks.append(
            "Desktop HOME saved file, ~/.config setting and /workspace file survive idle and "
            "forced container recreation; unsaved process memory is not claimed"
        )

    async def cleanup(self) -> list[str]:
        errors: list[str] = []
        for connection in self.connections:
            try:
                await asyncio.wait_for(connection.aclose(), timeout=3)
            except Exception as error:
                errors.append("detach: " + type(error).__name__)
        if self.process is not None and self.process.returncode is None:
            self.process.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=15)
            except asyncio.TimeoutError:
                self.process.send_signal(signal.SIGUSR1)
                self.process.send_signal(signal.SIGUSR2)
                await asyncio.sleep(0.2)
                self.process.kill()
                await self.process.wait()
                errors.append("Own service required forced shutdown")
        with closing(docker.from_env()) as client:
            for sid in self.sessions:
                try:
                    with suppress(docker.errors.NotFound):
                        container = client.containers.get("sandboxd-" + sid)
                        if container.name != "sandboxd-" + sid or not sid.startswith(self.prefix):
                            raise RuntimeError("Refusing cleanup outside this fixture")
                        container.remove(force=True)
                except Exception as error:
                    errors.append("container cleanup: " + type(error).__name__)
            if self.fixture is not None:
                try:
                    self.fixture.remove(force=True)
                except Exception as error:
                    errors.append("fixture cleanup: " + type(error).__name__)
            if self.network is not None:
                try:
                    self.network.remove()
                except Exception as error:
                    errors.append("network cleanup: " + type(error).__name__)
        credential = self.folder / "service" / "service-token"
        if credential.exists():
            credential.unlink()
        return errors

    async def run(self) -> None:
        try:
            await self.setup()
            await self.owner_lock()
            await self.browser_roundtrip()
            if not self.args.browser_only:
                await self.desktop_home()
            self.report["status"] = "passed"
        except BaseException as error:
            self.report.update(
                {
                    "status": "failed",
                    "failure_type": type(error).__name__,
                    "failure": str(error)[:2000],
                }
            )
            raise
        finally:
            success = self.report.get("status") == "passed"
            cleanup_errors = await self.cleanup()
            self.report["cleanup_errors"] = cleanup_errors
            if cleanup_errors:
                self.report["status"] = "failed"
            if self.folder.exists():
                (self.folder / "receipt.json").write_text(json.dumps(self.report, indent=2))
            if success and cleanup_errors:
                raise RuntimeError("Persistence acceptance cleanup failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--browser-image", required=True)
    parser.add_argument("--desktop-image", required=True)
    parser.add_argument("--browser-only", action="store_true")
    parser.add_argument("--sandbox-idle-seconds", type=int, default=3)
    asyncio.run(Acceptance(parser.parse_args()).run())


if __name__ == "__main__":
    main()
