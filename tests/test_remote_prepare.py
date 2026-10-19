"""`python -m pydantic_ai_backends.remote.prepare`, what a deploy runs first."""

from __future__ import annotations

import pytest

from pydantic_ai_backends.remote import prepare


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOXD_TOKEN", "t")


def test_every_runtime_ready_exits_zero(monkeypatch: pytest.MonkeyPatch, environment: None) -> None:
    seen: list[int] = []
    monkeypatch.setattr(
        prepare, "prepare_runtimes", lambda _config, *, attempts: seen.append(attempts) or []
    )

    assert prepare.main() == 0
    assert seen == [prepare.ATTEMPTS]


def test_a_runtime_left_unprepared_exits_one_and_names_it(
    monkeypatch: pytest.MonkeyPatch, environment: None, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(prepare, "prepare_runtimes", lambda _config, *, attempts: ["workbench"])

    assert prepare.main() == 1
    assert "Not prepared: workbench" in capsys.readouterr().err


def test_a_configuration_it_cannot_read_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SANDBOXD_TOKEN", raising=False)

    assert prepare.main() == 2
    assert "SANDBOXD_TOKEN" in capsys.readouterr().err
