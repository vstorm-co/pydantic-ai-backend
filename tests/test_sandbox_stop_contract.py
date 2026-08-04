"""One signature for `stop`, across every sandbox that has one.

Ending a sandbox is one idea, and it used to have three spellings:
`RemoteSandbox.stop(purge=False)`, `DockerSandbox.stop(remove=False)`, and
`DaytonaSandbox.stop()` / `KubernetesPodSandbox.stop()` taking nothing at all. A
caller holding "a sandbox" could not call it without knowing which one it had:

    >>> stop = getattr(sandbox, "stop")
    >>> stop(purge=True)
    TypeError: DaytonaSandbox.stop() got an unexpected keyword argument 'purge'

The failure was quiet in the worst possible place. Teardown is best-effort and
normally sits inside a broad `except`, so the `TypeError` was swallowed and
logged - and the call that would have released the resource was the one that
raised. In AgenticOS that meant a Daytona sandbox was never deleted on any path:
once per run, on the customer's own cloud account, until somebody read the bill.

So this file asserts the *shape* rather than any one backend's behaviour, which
is what the individual test modules already cover. A backend added later fails
here the moment it invents a fourth spelling.
"""

from __future__ import annotations

import inspect

import pytest

from pydantic_ai_backends.backends.base import AsyncBaseSandbox, BaseSandbox
from pydantic_ai_backends.backends.daytona import DaytonaSandbox
from pydantic_ai_backends.backends.docker.sandbox import DockerSandbox
from pydantic_ai_backends.backends.kubernetes import KubernetesPodSandbox
from pydantic_ai_backends.remote.client import RemoteSandbox

STOPPABLE = [
    BaseSandbox,
    AsyncBaseSandbox,
    DaytonaSandbox,
    DockerSandbox,
    KubernetesPodSandbox,
    RemoteSandbox,
]


@pytest.mark.parametrize("sandbox_type", STOPPABLE, ids=lambda cls: cls.__name__)
def test_stop_takes_purge_and_defaults_to_keeping_what_it_holds(sandbox_type: type) -> None:
    """Every `stop` accepts `purge`, and every one of them defaults to False.

    The default matters as much as the name. `stop()` with no argument is what a
    turn ending calls, and it must not discard files the next turn is meant to
    find - so a backend that defaulted to purging would silently lose work for a
    caller that had done nothing wrong.
    """
    parameters = inspect.signature(sandbox_type.stop).parameters

    assert "purge" in parameters, f"{sandbox_type.__name__}.stop has no `purge`"
    assert parameters["purge"].default is False


@pytest.mark.parametrize("sandbox_type", STOPPABLE, ids=lambda cls: cls.__name__)
def test_stop_can_be_called_with_purge_by_keyword(sandbox_type: type) -> None:
    """`purge` is reachable by name, which is how a generic caller passes it.

    Positionally it would work by accident on some of these and not others; the
    call that broke was `stop(purge=True)`.
    """
    parameters = inspect.signature(sandbox_type.stop).parameters

    assert parameters["purge"].kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }


def test_nothing_still_spells_it_remove() -> None:
    """`DockerSandbox` is the one that used to, and keeps it only as an alias.

    Honoured rather than removed, because this is a patch release and somebody's
    teardown passes it - but it warns, and it is the only place the old name is
    allowed to appear.
    """
    for sandbox_type in STOPPABLE:
        parameters = inspect.signature(sandbox_type.stop).parameters
        if sandbox_type is DockerSandbox:
            assert parameters["remove"].default is None
            continue
        assert "remove" not in parameters, f"{sandbox_type.__name__}.stop still takes `remove`"
