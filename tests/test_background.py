"""Tests for background (long-lived) process support."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from pydantic_ai_backends import (
    AsyncBackgroundSandboxAdapter,
    AsyncBackgroundSandboxProtocol,
    BackgroundSandboxProtocol,
    LocalBackend,
    create_console_toolset,
    ensure_async,
)
from pydantic_ai_backends.permissions import OperationPermissions, PermissionRuleset
from pydantic_ai_backends.toolsets.console import ConsoleDeps


def _wait_until_done(
    backend: LocalBackend, shell_id: str, timeout: float = 5.0
) -> tuple[str, int | None]:
    """Drain a shell until it exits; return (accumulated stdout, exit_code)."""
    deadline = time.monotonic() + timeout
    acc = ""
    while time.monotonic() < deadline:
        result = backend.read_background(shell_id)
        acc += result.stdout
        if not result.running:
            return acc, result.exit_code
        time.sleep(0.05)
    raise AssertionError(f"shell {shell_id} did not finish within {timeout}s")


class TestLocalBackendBackground:
    def test_protocol_membership(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=str(tmp_path))
        assert isinstance(backend, BackgroundSandboxProtocol)

    def test_incremental_read_and_exit_code(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=str(tmp_path))
        handle = backend.execute_background("echo first; sleep 0.4; echo second")
        assert handle.shell_id == "bg_1"
        assert handle.pid > 0

        # Give the first echo time to land, then read — should be running.
        time.sleep(0.2)
        first = backend.read_background(handle.shell_id)
        assert first.running is True
        assert "first" in first.stdout
        assert "second" not in first.stdout  # not produced yet

        # Drain to completion; the second chunk is new since the last read.
        rest, exit_code = _wait_until_done(backend, handle.shell_id)
        assert "second" in rest
        assert "first" not in rest  # already consumed by the first read
        assert exit_code == 0
        backend.kill_all_background()

    def test_read_unknown_shell(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=str(tmp_path))
        result = backend.read_background("bg_999")
        assert result.running is False
        assert "No such background shell" in result.stderr

    def test_kill_running_returns_true(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=str(tmp_path))
        handle = backend.execute_background("sleep 30")
        time.sleep(0.2)
        assert backend.kill_background(handle.shell_id) is True
        # After killing, it is no longer running.
        after = backend.read_background(handle.shell_id)
        assert after.running is False
        backend.kill_all_background()

    def test_kill_finished_returns_false(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=str(tmp_path))
        handle = backend.execute_background("echo done")
        _wait_until_done(backend, handle.shell_id)
        # Already exited → kill reports it was not running.
        assert backend.kill_background(handle.shell_id) is False
        backend.kill_all_background()

    def test_kill_unknown_returns_false(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=str(tmp_path))
        assert backend.kill_background("bg_404") is False

    def test_list_background(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=str(tmp_path))
        assert backend.list_background() == []
        h1 = backend.execute_background("sleep 30")
        h2 = backend.execute_background("echo hi")
        _wait_until_done(backend, h2.shell_id)
        infos = {i.shell_id: i for i in backend.list_background()}
        assert infos[h1.shell_id].running is True
        assert infos[h2.shell_id].running is False
        assert infos[h2.shell_id].exit_code == 0
        assert infos[h1.shell_id].command == "sleep 30"
        backend.kill_all_background()

    def test_kill_all_clears_and_removes_dir(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=str(tmp_path))
        backend.execute_background("sleep 30")
        bg_dir = backend._bg_dir
        assert bg_dir is not None and bg_dir.exists()
        backend.kill_all_background()
        assert backend.list_background() == []
        assert backend._bg_dir is None
        assert not bg_dir.exists()

    def test_kill_all_noop_when_nothing_started(self, tmp_path: Path) -> None:
        # No background process ever started → no temp dir to remove.
        backend = LocalBackend(root_dir=str(tmp_path))
        assert backend._bg_dir is None
        backend.kill_all_background()  # must not raise
        assert backend.list_background() == []

    def test_disabled_raises(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=str(tmp_path), enable_execute=False)
        with pytest.raises(RuntimeError, match="disabled"):
            backend.execute_background("echo nope")

    def test_permission_denied_raises(self, tmp_path: Path) -> None:
        ruleset = PermissionRuleset(execute=OperationPermissions(default="deny"))
        backend = LocalBackend(root_dir=str(tmp_path), permissions=ruleset)
        with pytest.raises(PermissionError):
            backend.execute_background("echo nope")


class TestAsyncBackgroundAdapter:
    def test_ensure_async_routes_to_background_adapter(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=str(tmp_path))
        adapter = ensure_async(backend)
        assert isinstance(adapter, AsyncBackgroundSandboxAdapter)
        assert isinstance(adapter, AsyncBackgroundSandboxProtocol)

    async def test_async_background_flow(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=str(tmp_path))
        adapter = ensure_async(backend)
        assert isinstance(adapter, AsyncBackgroundSandboxAdapter)

        handle = await adapter.execute_background("echo a; sleep 30")
        time.sleep(0.2)
        out = await adapter.read_background(handle.shell_id)
        assert "a" in out.stdout
        listed = await adapter.list_background()
        assert any(i.shell_id == handle.shell_id and i.running for i in listed)
        assert await adapter.kill_background(handle.shell_id) is True
        await adapter.kill_all_background()
        assert await adapter.list_background() == []


@dataclass
class _Deps:
    backend: LocalBackend


def _ctx(backend: LocalBackend) -> RunContext[ConsoleDeps]:
    return RunContext(deps=_Deps(backend=backend), model=TestModel(), usage=RunUsage())


class TestBackgroundTools:
    def test_tools_registered(self, tmp_path: Path) -> None:
        toolset = create_console_toolset()
        for name in ("run_in_background", "read_output", "kill_shell", "list_shells"):
            assert name in toolset.tools

    def test_tools_absent_when_disabled(self, tmp_path: Path) -> None:
        toolset = create_console_toolset(include_background=False)
        assert "run_in_background" not in toolset.tools
        assert "execute" in toolset.tools  # execute still present

    async def test_end_to_end_via_tools(self, tmp_path: Path) -> None:
        backend = LocalBackend(root_dir=str(tmp_path))
        toolset = create_console_toolset()
        ctx = _ctx(backend)
        tools = toolset.tools

        started = await tools["run_in_background"].function(ctx, "echo hello; sleep 30")
        assert "Started background shell bg_1" in started

        time.sleep(0.2)
        out = await tools["read_output"].function(ctx, "bg_1")
        assert "hello" in out
        assert "running" in out

        listing = await tools["list_shells"].function(ctx)
        assert "bg_1" in listing

        killed = await tools["kill_shell"].function(ctx, "bg_1")
        assert "Killed background shell bg_1" in killed
        backend.kill_all_background()
