"""The density benchmark's contract with the sandbox, without a daemon (AUD-1019-13).

The script is what sizing decisions are taken from, and it went on calling
`DockerSandbox.execute()` after the move to `CommandRunner` removed it. This
pins the calls it makes to the ones that exist.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from pydantic_ai_backends.types import CommandOutcome

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_density.py"


@pytest.fixture
def bench(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_density", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: its dataclasses look their module up by name.
    monkeypatch.setitem(sys.modules, "bench_density", module)
    spec.loader.exec_module(module)
    return module


class _Sandbox:
    def __init__(self, outcome: CommandOutcome, *, fail_start: bool = False) -> None:
        self.outcome = outcome
        self.fail_start = fail_start
        self.session_id = "s-1"
        self.ran: list[list[str]] = []
        self.removed = False

    async def run_command(self, argv: list[str], **kwargs: Any) -> CommandOutcome:
        assert kwargs["run_id"]
        self.ran.append(argv)
        return self.outcome

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("no image")

    def stop(self, remove: bool = False) -> None:
        self.removed = remove

    def resource_usage(self) -> None:
        return None


def test_the_first_command_goes_through_run_command(bench: ModuleType) -> None:
    sandbox = _Sandbox(CommandOutcome(stdout="ready\n", stderr="", exit_code=0))

    bench.first_command(sandbox)

    assert sandbox.ran == [["echo", "ready"]]


@pytest.mark.parametrize(
    "outcome",
    [
        CommandOutcome(stdout="", stderr="boom", exit_code=1),
        CommandOutcome(stdout="", stderr="", timed_out=True),
    ],
)
def test_a_sandbox_that_does_not_answer_is_an_error(
    bench: ModuleType, outcome: CommandOutcome
) -> None:
    with pytest.raises(RuntimeError, match="did not answer"):
        bench.first_command(_Sandbox(outcome))


def test_a_sandbox_that_fails_to_start_is_still_removed(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[_Sandbox] = []

    def sandbox(**_kwargs: Any) -> _Sandbox:
        made.append(_Sandbox(CommandOutcome(stdout="", stderr="", exit_code=0), fail_start=True))
        return made[-1]

    monkeypatch.setattr(bench, "DockerSandbox", sandbox)
    monkeypatch.setattr(bench, "mem_available_mib", lambda: 0.0)
    monkeypatch.setattr(bench, "management_rss_mib", lambda: 0.0)
    args = argparse.Namespace(
        sessions=1,
        runtime=None,
        prefix="bench",
        mem_limit=None,
        cpu_shares=None,
        oci_runtime=None,
        network_mode=None,
        image=None,
        settle=0,
        hibernate=False,
    )

    with pytest.raises(RuntimeError, match="no image"):
        bench.run(args)

    assert made[0].removed is True
