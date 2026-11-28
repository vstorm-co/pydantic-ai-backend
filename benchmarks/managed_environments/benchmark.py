"""Bounded local-wheel acceptance/benchmark. Creates and removes only its own resources."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import shlex
import statistics
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import closing, suppress
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

import docker
import httpx
from pydantic import BaseModel, ConfigDict
from pydantic_ai.workspaces import Workspace

from pydantic_ai_backends.browser import BrowserError, SandboxdBrowser
from pydantic_ai_backends.desktop import Key, Launch, TypeText, open_sandbox_desktop
from pydantic_ai_backends.remote.wire import SessionCreated
from pydantic_ai_backends.workspaces import SandboxdWorkspace

T = TypeVar("T")


class PrivateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    session_id: str
    token: str
    context_id: str
    target_id: str
    marker: str


def result_value(result: dict[str, object]) -> object:
    nested = result.get("result")
    if not isinstance(nested, dict) or "value" not in nested:
        raise AssertionError("Browser did not return a by-value result")
    return nested["value"]


def distribution(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)

    def percentile(q: float) -> float:
        offset = (len(ordered) - 1) * q
        low, high = math.floor(offset), math.ceil(offset)
        return ordered[low] + (ordered[high] - ordered[low]) * (offset - low)

    return {
        "n": len(values),
        "min_ms": min(values),
        "median_ms": statistics.median(values),
        "p95_ms": percentile(0.95),
        "max_ms": max(values),
    }


async def timed(operation: Callable[[], Awaitable[T]]) -> tuple[T, float]:
    begin = time.perf_counter()
    value = await operation()
    return value, (time.perf_counter() - begin) * 1000


async def samples(operation: Callable[[], Awaitable[object]], count: int) -> dict[str, float | int]:
    times = []
    for _ in range(count):
        _, duration = await timed(operation)
        times.append(duration)
    return distribution(times)


class ResourceSampler:
    def __init__(self, container_name: str, server_pid: int | None = None) -> None:
        self.name = container_name
        self.server_pid = server_pid
        self.values: list[dict[str, float | int]] = []
        self._stop = asyncio.Event()

    def _sample(self) -> dict[str, float | int]:
        # Docker stats is observational: it does not execute in the container.
        with closing(docker.from_env()) as client:
            container = client.containers.get(self.name)
            data = container.stats(stream=False)
            memory = data.get("memory_stats", {})
            usage = int(memory.get("usage", 0))
            detail = memory.get("stats", {})
            inactive = int(detail.get("inactive_file", detail.get("total_inactive_file", 0)))
            cpu = data.get("cpu_stats", {})
            previous = data.get("precpu_stats", {})
            delta = int(cpu.get("cpu_usage", {}).get("total_usage", 0)) - int(
                previous.get("cpu_usage", {}).get("total_usage", 0)
            )
            system_delta = int(cpu.get("system_cpu_usage", 0)) - int(
                previous.get("system_cpu_usage", 0)
            )
            online = int(cpu.get("online_cpus", 1))
            sample = {
                "observed_monotonic": time.monotonic(),
                "memory_usage_bytes": usage,
                "working_set_bytes": max(0, usage - inactive),
                "cpu_one_core_percent": (delta / system_delta * online * 100)
                if system_delta > 0
                else 0,
                "pids": int(data.get("pids_stats", {}).get("current", 0)),
                "memory_limit_bytes": int(memory.get("limit", 0)),
                "cpu_throttled_ns_total": int(
                    cpu.get("throttling_data", {}).get("throttled_time", 0)
                ),
            }
            if self.server_pid is not None:
                row = subprocess.check_output(
                    ["ps", "-o", "time=,rss=", "-p", str(self.server_pid)], text=True
                ).split()
                elapsed, rss = row
                segments = elapsed.split(":")
                cpu_seconds = sum(
                    float(part) * (60**index) for index, part in enumerate(reversed(segments))
                )
                sample["server_cpu_seconds_total"] = cpu_seconds
                sample["server_rss_bytes"] = int(rss) * 1024
            return sample

    async def run(self) -> None:
        while not self._stop.is_set():
            self.values.append(await asyncio.to_thread(self._sample))
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=0.25)

    def stop(self) -> None:
        self._stop.set()


async def child_reconnect(private: Path) -> None:
    saved = PrivateSession.model_validate_json(private.read_bytes())
    client = SandboxdBrowser(saved.base_url, saved.session_id, saved.token)
    connection = await client.connect(context_id=saved.context_id)
    try:
        tab = await connection.browser.tab(saved.target_id)
        observed = result_value(
            await tab.evaluate(
                "JSON.stringify({marker:window.qaMarker,counter:window.qaCoun"
                "ter,value:document.querySelector('#qa').value})"
            )
        )
        assert observed == json.dumps(
            {"marker": saved.marker, "counter": 7, "value": "unsaved-" + saved.marker},
            separators=(",", ":"),
        ), observed
    finally:
        await connection.aclose()
    print(json.dumps({"fresh_process_exact_state": True}))


class Benchmark:
    def __init__(
        self,
        folder: Path,
        base_url: str,
        count: int,
        wheel: Path,
        skip_browser: bool = False,
        desktop_only: bool = False,
    ) -> None:
        self.folder, self.base_url, self.count = folder, base_url, count
        self.skip_browser = skip_browser
        self.desktop_only = desktop_only
        self.receipt_path = folder / (
            "receipt-desktop-pooled.json"
            if desktop_only
            else ("receipt-expiry-desktop.json" if skip_browser else "receipt.json")
        )
        import pydantic_ai_backends

        imported = Path(pydantic_ai_backends.__file__).resolve()
        if "/site-packages/" not in str(imported):
            raise RuntimeError("Benchmark requires an installed wheel, not editable source")
        pid_file = folder / "server-pid"
        self.server_pid = int(pid_file.read_text()) if pid_file.exists() else None
        self.prefix = "qa-bench-" + uuid4().hex[:10]
        self.service_token = (folder / "service-token").read_text()
        self.sessions: list[str] = []
        with closing(docker.from_env()) as hardware:
            info = hardware.info()
            daemon = {
                key: info.get(key)
                for key in [
                    "ID",
                    "Name",
                    "Architecture",
                    "OSType",
                    "NCPU",
                    "MemTotal",
                    "ServerVersion",
                ]
            }
        self.report: dict[str, object] = {
            "model_calls": 0,
            "synthetic": True,
            "sample_count": count,
            "scope": "Desktop only"
            if desktop_only
            else ("expiry and Desktop only" if skip_browser else "Browser, expiry and Desktop"),
            "wheel": str(wheel.resolve()),
            "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "installed_module": str(imported),
            "installed_version": pydantic_ai_backends.__version__,
            "python": sys.version,
            "platform": platform.platform(),
            "docker_daemon": daemon,
            "timing_scope": (
                "Cached runtime image: fresh container/GUI startup; not cold image pull or build"
            ),
            "limits": {
                "max_simultaneous_containers": 2,
                "cpus_per_container": 1,
                "memory_per_container": "1g",
                "network": "none",
            },
            "checks": [],
            "timings": {},
            "resources": {},
        }

    def check(self, text: str) -> None:
        checks = self.report["checks"]
        assert isinstance(checks, list)
        checks.append(text)

    def timing(self, key: str, value: object) -> None:
        timings = self.report["timings"]
        assert isinstance(timings, dict)
        timings[key] = value

    async def resource_phase(
        self,
        container_name: str,
        label: str,
        operation: Callable[[], Awaitable[object]],
        *,
        fps: float,
        seconds: float = 6,
    ) -> None:
        sampler = ResourceSampler(container_name, self.server_pid)
        task = asyncio.create_task(sampler.run())
        count = 0
        timings: list[float] = []
        started = time.monotonic()
        active_end = started
        try:
            while time.monotonic() - started < seconds:
                tick = time.monotonic()
                _, elapsed = await timed(operation)
                timings.append(elapsed)
                count += 1
                await asyncio.sleep(max(0, 1 / fps - (time.monotonic() - tick)))
        finally:
            active_end = time.monotonic()
            sampler.stop()
            await task
        resources = self.report["resources"]
        assert isinstance(resources, dict)
        resources[label] = {
            "requested_fps": fps,
            "duration_seconds": active_end - started,
            "sampling_span_seconds": time.monotonic() - started,
            "frames": count,
            "latency": distribution(timings),
            "samples": sampler.values,
        }

    async def create(self, runtime: str, suffix: str, tenant: str = "qa-bench-a") -> SessionCreated:
        sid = self.prefix + "-" + suffix
        self.sessions.append(sid)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=120) as client:
            response = await client.post(
                "/sessions",
                headers={"X-Sandbox-Token": self.service_token},
                json={"session_id": sid, "runtime": runtime, "tenant": tenant},
            )
            response.raise_for_status()
            return SessionCreated.model_validate_json(response.content)

    async def delete(self, sid: str) -> None:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            response = await client.delete(
                "/sessions/" + sid, headers={"X-Sandbox-Token": self.service_token}
            )
            assert response.status_code in (204, 404)

    async def browser(self) -> None:
        session, elapsed = await timed(lambda: self.create("browser", "browser-a"))
        self.timing("browser_cached_image_fresh_container_ms", elapsed)
        api = SandboxdBrowser(self.base_url, session.session.session_id, session.token)
        _, elapsed = await timed(api.start)
        self.timing("browser_first_gui_start_ms", elapsed)
        connection, elapsed = await timed(api.connect)
        self.timing("browser_first_context_connect_ms", elapsed)
        tab = await connection.browser.new_tab()
        await tab.resize(1024, 768)
        marker = uuid4().hex
        expression = (
            (
                "document.body.innerHTML='<form onsubmit=\"window.qaSubmitted="
                '(window.qaSubmitted||0)+1;return false"><input id=qa autofoc'
                "us><button>submit</button></form>';window.qaMarker="
            )
            + json.dumps(marker)
            + ";window.qaCounter=7;document.querySelector('#qa').focus();true"
        )
        assert result_value(await tab.evaluate(expression)) is True
        await tab.type_text("unsaved-" + marker)
        private = self.folder / "browser-reconnect-private.json"
        private.write_text(
            PrivateSession(
                base_url=self.base_url,
                session_id=session.session.session_id,
                token=session.token,
                context_id=connection.browser.context_id,
                target_id=tab.id,
                marker=marker,
            ).model_dump_json()
        )
        private.chmod(0o600)
        context_id, target_id = connection.browser.context_id, tab.id
        await connection.aclose()
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            __file__,
            "--reconnect-private",
            str(private),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=30)
        assert child.returncode == 0, stderr.decode()[-2000:]
        assert json.loads(stdout)["fresh_process_exact_state"] is True
        self.check(
            "Browser exact DOM, unsaved form and JS counter retained afte"
            "r original client disconnect and fresh process reconnect"
        )
        connection = await api.connect(context_id=context_id)
        tab = await connection.browser.tab(target_id)
        frame = await tab.screenshot()
        (self.folder / "browser.png").write_bytes(frame)
        state = await api.inspect()
        sampler = ResourceSampler("sandboxd-" + session.session.session_id, self.server_pid)
        task = asyncio.create_task(sampler.run())
        try:
            await asyncio.sleep(4)
        finally:
            sampler.stop()
            await task
        after_idle = await api.inspect()
        assert after_idle.last_activity_at == state.last_activity_at
        self.check("Passive zero-view wait and state inspection do not renew Browser idle deadline")
        resources = self.report["resources"]
        assert isinstance(resources, dict)
        resources["browser_idle"] = sampler.values
        sampler = ResourceSampler("sandboxd-" + session.session.session_id, self.server_pid)
        task = asyncio.create_task(sampler.run())
        try:
            self.timing("browser_screenshot", await samples(tab.screenshot, self.count))
            await asyncio.sleep(2)
        finally:
            sampler.stop()
            await task
        resources["browser_screenshot_loop"] = sampler.values
        await self.resource_phase(
            "sandboxd-" + session.session.session_id, "browser_live_1fps", tab.screenshot, fps=1
        )
        await self.resource_phase(
            "sandboxd-" + session.session.session_id, "browser_live_10fps", tab.screenshot, fps=10
        )
        passive_after = await api.inspect()
        assert passive_after.last_activity_at == after_idle.last_activity_at
        self.check("Repeated passive screenshots do not renew Browser idle deadline")
        self.timing(
            "browser_evaluate_small",
            await samples(lambda: tab.evaluate("window.qaCounter"), self.count),
        )
        self.timing(
            "browser_type_text_one_character", await samples(lambda: tab.type_text("x"), self.count)
        )
        assert (await api.inspect()).last_activity_at > passive_after.last_activity_at
        self.check("Actual physical input renews Browser idle deadline")
        await tab.evaluate("document.querySelector('#qa').focus()")
        await tab.key("Enter")
        assert result_value(await tab.evaluate("window.qaSubmitted")) == 1
        self.check("Real Chromium Enter submits the controlled form")
        reconnect_times = []
        for _ in range(self.count):
            await connection.aclose()
            connection, elapsed = await timed(lambda: api.connect(context_id=context_id))
            reconnect_times.append(elapsed)
            tab = await connection.browser.tab(target_id)
        self.timing("browser_existing_context_reconnect", distribution(reconnect_times))
        await asyncio.sleep(1)
        with closing(docker.from_env()) as client:
            container = client.containers.get("sandboxd-" + session.session.session_id)
            probe = """import os
from pathlib import Path
matches = []
for path in Path('/proc').iterdir():
    if not path.name.isdigit() or int(path.name) == os.getpid():
        continue
    try:
        command = (path / 'cmdline').read_bytes()
    except FileNotFoundError:
        continue
    if b'websocket.create_connection' in command:
        matches.append(path.name)
print(len(matches))
"""
            reply = container.exec_run(["python3", "-c", probe])
            assert reply.exit_code == 0
            live_bridges = int(reply.output.decode().strip())
            resources["browser_live_bridges_after_25_reconnects"] = live_bridges
            assert live_bridges == 1, live_bridges
        self.check(
            "Repeated reconnect leaves exactly one live bridge, without a"
            "bandoned per-viewer processes"
        )

        second, elapsed = await timed(lambda: self.create("browser", "browser-b", "qa-bench-b"))
        self.timing("browser_second_fresh_container_ms", elapsed)
        api_b = SandboxdBrowser(self.base_url, second.session.session_id, second.token)
        await api_b.start()
        conn_b = await api_b.connect()
        tab_b = await conn_b.browser.new_tab()
        try:
            await tab_b.evaluate(
                "window.qaMarker='second-context';document.body.textContent='second'"
            )
            assert result_value(await tab.evaluate("window.qaMarker")) == marker
            try:
                await connection.browser.tab(tab_b.id)
            except BrowserError:
                pass
            else:
                raise AssertionError("Foreign Browser context accepted")
            async with httpx.AsyncClient() as client:
                forbidden = await client.get(
                    self.base_url
                    + "/sessions/"
                    + session.session.session_id
                    + "/environments/browser",
                    headers={"X-Sandbox-Token": second.token},
                )
            assert forbidden.status_code in (401, 403)
            self.check(
                "Two tenants have separate containers/context state; wrong to"
                "ken and foreign target attachment refused"
            )
            with closing(docker.from_env()) as client:
                containers = [
                    client.containers.get("sandboxd-" + s.session.session_id)
                    for s in (session, second)
                ]
                identities = []
                for container in containers:
                    attrs = container.attrs
                    assert attrs["HostConfig"]["NetworkMode"] == "none"
                    assert attrs["HostConfig"]["NanoCpus"] == 1_000_000_000
                    assert attrs["HostConfig"]["Memory"] == 1_073_741_824
                    assert not attrs["NetworkSettings"]["Ports"]
                    identities.append(container.id)
                assert len(set(identities)) == 2
            self.check(
                "Actual Docker limits verified: 1CPU/1GiB, networknone and no"
                " published ports per session"
            )
            # A dead mandatory proxy and networknone block private and public navigation.
            for url in ("http://169.254.169.254/latest/meta-data/", "https://example.com/"):
                try:
                    await tab_b.navigate(url)
                except BrowserError:
                    pass
                else:
                    raise AssertionError("Offline protected Browser unexpectedly navigated: " + url)
            self.check(
                "Independent network isolation rejects metadata/private and p"
                "ublic egress with unreachable mandatory proxy"
            )
        finally:
            await conn_b.aclose()
            await api_b.end()
        # Kill only our owned browser container; reads may not silently heal it.
        with closing(docker.from_env()) as client:
            original = client.containers.get("sandboxd-" + session.session.session_id)
            original_id = original.id
            original.kill()
        await connection.aclose()
        try:
            await api.inspect()
        except BrowserError:
            pass
        else:
            raise AssertionError("Dead Browser reported ready")
        with closing(docker.from_env()) as client:
            found = client.containers.get("sandboxd-" + session.session.session_id)
            found.reload()
            assert found.id == original_id and found.status != "running"
        self.check("Killed exact Browser refuses inspect; no replacement or wake occurs")
        await api.end()

    async def expiry(self, *, hard: bool) -> None:
        session = await self.create("browser", "hard-expiry" if hard else "idle-expiry")
        api = SandboxdBrowser(self.base_url, session.session.session_id, session.token)
        await api.start(idle_seconds=900 if hard else 3, lifetime_seconds=3 if hard else 3600)
        connection = await api.connect()
        try:
            tab = await connection.browser.new_tab()
            await tab.evaluate("document.body.textContent='expiry test'")
            deadline = time.monotonic() + 8
            refused = False
            while time.monotonic() < deadline:
                try:
                    await tab.screenshot()
                    await api.inspect()
                except BrowserError:
                    refused = True
                    break
                await asyncio.sleep(0.2)
            assert refused, "Passive frames prevented expiry"
        finally:
            await connection.aclose()
        retired = time.monotonic()
        with closing(docker.from_env()) as client:
            container = client.containers.get("sandboxd-" + session.session.session_id)
            stop_deadline = time.monotonic() + 4
            while time.monotonic() < stop_deadline:
                container.reload()
                if container.status != "running":
                    break
                await asyncio.sleep(0.1)
            assert container.status != "running"
        self.timing(
            "browser_" + ("hard" if hard else "idle") + "_provider_stop_after_refusal_ms",
            (time.monotonic() - retired) * 1000,
        )
        self.check(
            ("Hard lifetime" if hard else "Idle deadline")
            + (
                " revokes passive access, then sweep stops only its container"
                " within bounded cleanup window"
            )
        )

    async def desktop(self) -> None:
        session, elapsed = await timed(lambda: self.create("desktop", "desktop"))
        self.timing("desktop_cached_image_fresh_container_ms", elapsed)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=120) as client:
            begin = time.perf_counter()
            response = await client.post(
                "/sessions/" + session.session.session_id + "/environments/desktop",
                headers={"X-Sandbox-Token": session.token},
                json={"width": 1024, "height": 768},
            )
            response.raise_for_status()
            self.timing("desktop_first_gui_start_ms", (time.perf_counter() - begin) * 1000)
        capability = SandboxdWorkspace(
            service_url=self.base_url, token=self.service_token, runtime="desktop"
        )
        from pydantic_ai.workspaces import WorkspaceRef

        workspace = Workspace(
            capability.backend(WorkspaceRef(provider="sandboxd", id=session.session.session_id))
        )
        desktop = await open_sandbox_desktop(workspace, width=1024, height=768)
        apps = await desktop.apps()
        terminal = next(app for app in apps if app.id == "xfce4-terminal")
        await desktop.perform(Launch(app=terminal.id))
        marker = self.prefix + "-desktop"
        command = "printf %s " + shlex.quote(marker) + " > /workspace/retained-marker"
        await desktop.perform(TypeText(text=command))
        frame = await desktop.capture()
        (self.folder / "desktop-before.png").write_bytes(frame.image)
        sampler = ResourceSampler("sandboxd-" + session.session.session_id, self.server_pid)
        task = asyncio.create_task(sampler.run())
        try:
            self.timing("desktop_screenshot", await samples(desktop.capture, self.count))
            await asyncio.sleep(2)
        finally:
            sampler.stop()
            await task
        resources = self.report["resources"]
        assert isinstance(resources, dict)
        resources["desktop_screenshot_loop"] = sampler.values
        await self.resource_phase(
            "sandboxd-" + session.session.session_id, "desktop_live_1fps", desktop.capture, fps=1
        )
        await self.resource_phase(
            "sandboxd-" + session.session.session_id, "desktop_live_5fps", desktop.capture, fps=5
        )
        requests: list[str] = []

        async def trace(request: httpx.Request) -> None:
            requests.append(request.method + " " + request.url.path)

        async with httpx.AsyncClient(event_hooks={"request": [trace]}) as shared_http:
            pooled_capability = SandboxdWorkspace(
                service_url=self.base_url,
                token=self.service_token,
                runtime="desktop",
                client=shared_http,
            )
            pooled_workspace = Workspace(
                pooled_capability.backend(
                    WorkspaceRef(provider="sandboxd", id=session.session.session_id)
                )
            )
            pooled_desktop = await open_sandbox_desktop(pooled_workspace, width=1024, height=768)
            await pooled_desktop.capture()  # exclude lazy attach/policy initialization
            requests.clear()
            self.timing(
                "desktop_screenshot_pooled_http", await samples(pooled_desktop.capture, self.count)
            )
            resources["desktop_pooled_capture_requests"] = {
                "captures": self.count,
                "requests": len(requests),
                "paths": dict(Counter(requests)),
            }
            await pooled_desktop.aclose()

        await desktop.aclose()
        fresh = Workspace(
            capability.backend(WorkspaceRef(provider="sandboxd", id=session.session.session_id))
        )
        desktop, elapsed = await timed(lambda: open_sandbox_desktop(fresh, width=1024, height=768))
        self.timing("desktop_same_gui_reattach_ms", elapsed)
        await desktop.perform(Key(keys="Return"))
        result = await fresh.run(["cat", "/workspace/retained-marker"])
        assert result.stdout == marker
        self.check(
            "Desktop real terminal retains unsent command across adapter "
            "detach; Enter executes it after exact reattach"
        )
        self.timing(
            "desktop_keyboard_chord",
            await samples(lambda: desktop.perform(Key(keys="Shift")), self.count),
        )
        listing = await fresh.run(
            ["find", "/tmp", "-maxdepth", "1", "-name", "pydantic-desktop-screen-*"]
        )
        assert not listing.stdout.strip()
        self.check("Repeated Desktop screenshots leave no temporary image files")
        await desktop.aclose()
        await self.delete(session.session.session_id)
        marker_file = (
            self.folder
            / "workspaces"
            / session.session.session_id
            / "workspace"
            / "retained-marker"
        )
        assert marker_file.read_text() == marker
        self.check(
            "Explicit sandbox retirement keeps persisted workspace file while stopping resident GUI"
        )

    async def cleanup(self) -> None:
        errors: list[str] = []
        for sid in self.sessions:
            try:
                await self.delete(sid)
            except Exception as error:
                errors.append("Cleanup API outcome: " + type(error).__name__)
        with closing(docker.from_env()) as client:
            removed = []
            for sid in self.sessions:
                try:
                    container = client.containers.get("sandboxd-" + sid)
                except docker.errors.NotFound:
                    continue
                if container.name != "sandboxd-" + sid or not sid.startswith(self.prefix):
                    raise RuntimeError("Refusing to remove a container outside this benchmark")
                container.remove(force=True)
                removed.append(sid)
            self.report["own_containers_removed"] = removed
        for private in self.folder.glob("*private.json"):
            private.unlink()
        if errors:
            raise RuntimeError("; ".join(errors))

    async def run(self) -> None:
        try:
            if not self.skip_browser and not self.desktop_only:
                await self.browser()
            if not self.desktop_only:
                await self.expiry(hard=False)
                await self.expiry(hard=True)
            await self.desktop()
            self.report["status"] = "passed"
        except BaseException as error:
            self.report["status"] = "failed"
            self.report["failure_type"] = type(error).__name__
            self.report["failure"] = str(error)[:4000]
            raise
        finally:
            success_before_cleanup = self.report.get("status") == "passed"
            self.receipt_path.write_text(json.dumps(self.report, indent=2))
            try:
                await self.cleanup()
            except Exception as error:
                self.report["cleanup_failure"] = type(error).__name__ + ": " + str(error)
                self.report["status"] = "failed"
            self.receipt_path.write_text(json.dumps(self.report, indent=2))
            if success_before_cleanup and "cleanup_failure" in self.report:
                raise RuntimeError("Benchmark cleanup failed; inspect the receipt")


async def execute(args: argparse.Namespace) -> None:
    if args.reconnect_private:
        await child_reconnect(args.reconnect_private)
        return
    benchmark = Benchmark(
        args.folder, args.base_url, args.samples, args.wheel, args.skip_browser, args.desktop_only
    )
    await benchmark.run()
    print(json.dumps({"folder": str(args.folder), "status": benchmark.report["status"]}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--desktop-only", action="store_true")
    parser.add_argument("--reconnect-private", type=Path)
    args = parser.parse_args()
    if not 20 <= args.samples <= 50:
        parser.error("Use 20–50 samples; this is a bounded local benchmark")
    if not args.reconnect_private and (
        args.folder is None or args.base_url is None or args.wheel is None
    ):
        parser.error("--folder, --base-url and --wheel are required for a benchmark run")
    asyncio.run(execute(args))


if __name__ == "__main__":
    main()
