"""Building a :class:`~pydantic_ai_backends.remote.server.SandboxdConfig` from the environment.

The service's entrypoint used to read four variables — the token, the runtime
allowlist, the host and the port — while `SandboxdConfig` models thirty. So
every deployment that needed any of the other twenty-six wrote its own launcher
in Python, and each one got a different subset right.

The gap mattered most where its absence is silent. A service started without
`workspace_root` keeps files in the container's write layer, so an idle reaping
discards them and the next request opens an empty workspace — no error, nothing
in a log, an agent confidently referring to a file that is gone. A setting that
cannot be reached from a compose file is a setting most operators will never
set.

Everything here is a pure function of a mapping, so a deployment's configuration
can be asserted in a test rather than discovered by starting a container. The
only thing left in `run()` is uvicorn.

Every field of `SandboxdConfig` is `SANDBOXD_` plus its name in upper case, with
no renaming or abbreviation: one vocabulary for the dataclass, the environment
and the documentation, so there is no translation table to drift.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from pydantic_ai_backends.remote.server import (
    DEFAULT_RUNTIMES,
    SandboxdConfig,
    SandboxRuntime,
)
from pydantic_ai_backends.types import RuntimeConfig

PREFIX = "SANDBOXD_"
"""Prefix every variable carries."""

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

_RUNTIME_EXAMPLE = (
    "Example: SANDBOXD_RUNTIMES=python=python:3.12-slim,data=@python-datascience;mem_limit=4g"
)

T = TypeVar("T")


class SandboxdConfigError(ValueError):
    """The environment does not describe a service that could start.

    A `ValueError` rather than `SystemExit`: turning a bad configuration into an
    exit code is the entrypoint's decision, and a caller embedding the service
    in its own application wants to catch this, not be terminated by it.
    """


def config_from_env(env: Mapping[str, str]) -> SandboxdConfig:
    """The service configuration these variables describe.

    Args:
        env: Usually `os.environ`. Taken as an argument so a configuration can
            be tested without touching the process.

    Returns:
        A validated `SandboxdConfig`. Absent variables take the dataclass
        default, so an empty environment (bar the token) is the shipped policy.

    Raises:
        SandboxdConfigError: If a variable is missing, unparseable, or describes
            a combination the service refuses.
    """
    token = _text(env, "TOKEN", "")
    if not token:
        raise SandboxdConfigError(
            f"{PREFIX}TOKEN is required. It authorises opening a session, and a "
            "session can run commands on this host — generate a long random value."
        )

    # A sentinel instance, read for the shipped value of each scalar field so
    # this module does not keep a second copy of thirty defaults to drift from.
    # `default_runtime` is deliberately *not* taken from it: `__post_init__`
    # resolves an empty one to the first entry of the allowlist, so reading it
    # back would pin every deployment to the first of the *shipped* allowlist
    # and refuse any custom one. `test_env.py` guards the rest against becoming
    # derived in the same way.
    defaults = SandboxdConfig(token=token, runtimes=DEFAULT_RUNTIMES)
    raw_runtimes = env.get(PREFIX + "RUNTIMES")
    try:
        return SandboxdConfig(
            token=token,
            runtimes=(DEFAULT_RUNTIMES if raw_runtimes is None else _parse_runtimes(raw_runtimes)),
            default_runtime=_text(env, "DEFAULT_RUNTIME", ""),
            max_sessions=_optional(env, "MAX_SESSIONS", defaults.max_sessions, _as_int),
            max_open_sessions=_optional(
                env, "MAX_OPEN_SESSIONS", defaults.max_open_sessions, _as_int
            ),
            max_sessions_per_tenant=_optional(
                env, "MAX_SESSIONS_PER_TENANT", defaults.max_sessions_per_tenant, _as_int
            ),
            evict_idle_after=_optional(env, "EVICT_IDLE_AFTER", defaults.evict_idle_after, _as_int),
            mem_limit=_optional(env, "MEM_LIMIT", defaults.mem_limit, str),
            memswap_limit=_optional(env, "MEMSWAP_LIMIT", defaults.memswap_limit, str),
            cpus=_optional(env, "CPUS", defaults.cpus, _as_float),
            cpu_shares=_optional(env, "CPU_SHARES", defaults.cpu_shares, _as_int),
            pids_limit=_optional(env, "PIDS_LIMIT", defaults.pids_limit, _as_int),
            tmpfs_size=_optional(env, "TMPFS_SIZE", defaults.tmpfs_size, str),
            network_mode=_optional(env, "NETWORK_MODE", defaults.network_mode, str),
            oci_runtime=_optional(env, "OCI_RUNTIME", defaults.oci_runtime, str),
            work_dir=_required_text(env, "WORK_DIR", defaults.work_dir),
            workspace_root=_optional(env, "WORKSPACE_ROOT", defaults.workspace_root, str),
            persist_containers=_flag(env, "PERSIST_CONTAINERS", defaults.persist_containers),
            prewarm=_flag(env, "PREWARM", defaults.prewarm),
            idle_timeout=_value(env, "IDLE_TIMEOUT", defaults.idle_timeout, _as_int),
            cleanup_interval=_value(env, "CLEANUP_INTERVAL", defaults.cleanup_interval, _as_int),
            workspace_ttl=_optional(env, "WORKSPACE_TTL", defaults.workspace_ttl, _as_int),
            container_ttl=_optional(env, "CONTAINER_TTL", defaults.container_ttl, _as_int),
            sandbox_uid=_optional(env, "SANDBOX_UID", defaults.sandbox_uid, _as_int),
            max_read_bytes=_value(env, "MAX_READ_BYTES", defaults.max_read_bytes, _as_int),
            execute_timeout=_value(env, "EXECUTE_TIMEOUT", defaults.execute_timeout, _as_int),
            max_workers=_value(env, "MAX_WORKERS", defaults.max_workers, _as_int),
            ui_enabled=_flag(env, "UI_ENABLED", defaults.ui_enabled),
        )
    except ValueError as e:
        # `SandboxdConfig` refuses combinations no single variable is wrong in —
        # `evict_idle_after` without `workspace_root`, a `default_runtime` no
        # entry defines. Its messages name the fields, which are the variables
        # in lower case, so they are re-raised rather than paraphrased.
        raise SandboxdConfigError(str(e)) from e


def bind_from_env(env: Mapping[str, str]) -> tuple[str, int]:
    """The address to serve on: `SANDBOXD_HOST` and `SANDBOXD_PORT`.

    Separate from the service policy because it is not part of it — the same
    configuration served on a different port is the same service.
    """
    return _text(env, "HOST", "127.0.0.1"), _value(env, "PORT", 8080, _as_int)


def _value(env: Mapping[str, str], name: str, default: T, parse: Callable[[str], T]) -> T:
    """A variable that has no "unset" state: absent takes the default."""
    raw = env.get(PREFIX + name)
    if raw is None:
        return default
    return _parsed(name, raw, parse)


def _optional(
    env: Mapping[str, str], name: str, default: T | None, parse: Callable[[str], T]
) -> T | None:
    """A variable whose field is optional, where **empty means `None`**.

    The distinction earns its keep on the ceilings: absent leaves the shipped
    default in place, and `SANDBOXD_CPUS=` says "no hard CPU ceiling", which is
    a real choice on a host where `cpu_shares` should do the bounding. Without
    it there would be no way to turn a defaulted limit off from a compose file.
    """
    raw = env.get(PREFIX + name)
    if raw is None:
        return default
    if not raw:
        return None
    return _parsed(name, raw, parse)


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    return env.get(PREFIX + name, default)


def _required_text(env: Mapping[str, str], name: str, default: str) -> str:
    """A string field with no meaningful empty value."""
    value = env.get(PREFIX + name, default)
    if not value:
        raise SandboxdConfigError(f"{PREFIX}{name} must not be empty")
    return value


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(PREFIX + name)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise SandboxdConfigError(
        f"{PREFIX}{name}={raw!r} is not a boolean. Use one of {', '.join(sorted(_TRUE | _FALSE))}."
    )


def _parsed(name: str, raw: str, parse: Callable[[str], T]) -> T:
    try:
        return parse(raw)
    except ValueError as e:
        raise SandboxdConfigError(f"{PREFIX}{name}={raw!r} is not valid: {e}") from e


def _as_int(raw: str) -> int:
    return int(raw.strip())


def _as_float(raw: str) -> float:
    return float(raw.strip())


def _parse_runtimes(raw: str) -> dict[str, SandboxRuntime]:
    """The allowlist these variables describe, in either accepted form.

    A value starting with `{` is JSON, which is the only form that can express a
    runtime building a package list of its own. Anything else is the compact
    form, which covers what a compose file usually wants.

    `[` routes to JSON as well, though an array is never valid: somebody who
    reached for JSON and wrote a list should be told the shape is wrong, not
    that their array is not `alias=image`.
    """
    if raw.lstrip().startswith(("{", "[")):
        return _runtimes_from_json(raw)
    return _runtimes_from_pairs(raw)


def _runtimes_from_pairs(raw: str) -> dict[str, SandboxRuntime]:
    """`alias=source[;field=value...]`, comma separated.

    `source` is an image, or `@name` for one of the built-in `RuntimeConfig`s
    whose packages are built on first use. The modifier names are
    `SandboxRuntime`'s own fields, so there is nothing extra to learn and
    nothing to keep in step with it.
    """
    runtimes: dict[str, SandboxRuntime] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        head, _, modifiers = entry.partition(";")
        alias, sep, source = head.partition("=")
        alias = alias.strip()
        if not sep or not alias:
            raise SandboxdConfigError(
                f"{PREFIX}RUNTIMES entry {entry!r} is not 'alias=image'. {_RUNTIME_EXAMPLE}"
            )

        fields: dict[str, Any] = _runtime_source(source.strip(), f"entry {entry!r}")
        for modifier in modifiers.split(";"):
            modifier = modifier.strip()
            if not modifier:
                continue
            key, sep, value = modifier.partition("=")
            if not sep:
                raise SandboxdConfigError(
                    f"{PREFIX}RUNTIMES modifier {modifier!r} on {alias!r} is not "
                    f"'field=value'. {_RUNTIME_EXAMPLE}"
                )
            fields[key.strip()] = value.strip()
        runtimes[alias] = _build_runtime(alias, fields)
    if not runtimes:
        raise SandboxdConfigError(
            f"{PREFIX}RUNTIMES is set but names no runtime. Unset it to take the "
            f"shipped allowlist. {_RUNTIME_EXAMPLE}"
        )
    return runtimes


def _runtime_source(source: str, where: str) -> dict[str, Any]:
    """Whether this entry pulls an image or builds a named runtime.

    An empty source is refused here rather than passed on: `SandboxRuntime`
    checks that exactly one of `image` and `runtime` is set, which an empty
    string satisfies — so `{"py": ""}` would have been accepted as a runtime
    that pulls an image with no name, and failed on first use instead.
    """
    name = source[1:] if source.startswith("@") else source
    if not name:
        raise SandboxdConfigError(
            f"{PREFIX}RUNTIMES {where} names no image or runtime. {_RUNTIME_EXAMPLE}"
        )
    if source.startswith("@"):
        return {"runtime": name}
    return {"image": name}


def _runtimes_from_json(raw: str) -> dict[str, SandboxRuntime]:
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SandboxdConfigError(f"{PREFIX}RUNTIMES is not valid JSON: {e}") from e
    if not isinstance(loaded, dict):
        raise SandboxdConfigError(f"{PREFIX}RUNTIMES as JSON must be an object of aliases")

    runtimes: dict[str, SandboxRuntime] = {}
    for alias, entry in loaded.items():  # pyright: ignore[reportUnknownVariableType]
        if isinstance(entry, str):
            runtimes[str(alias)] = _build_runtime(
                str(alias), _runtime_source(entry, f"entry {alias!r}")
            )
            continue
        if not isinstance(entry, dict):
            raise SandboxdConfigError(
                f"{PREFIX}RUNTIMES entry {alias!r} must be an image string or an object"
            )
        fields = dict(entry)  # pyright: ignore[reportUnknownArgumentType]
        nested = fields.get("runtime")
        if isinstance(nested, dict):
            # The one thing the compact form cannot say: a runtime whose package
            # list is written here rather than named from the built-ins.
            try:
                fields["runtime"] = RuntimeConfig(**nested)  # pyright: ignore[reportUnknownArgumentType]
            except Exception as e:
                raise SandboxdConfigError(
                    f"{PREFIX}RUNTIMES entry {alias!r} has an invalid runtime: {e}"
                ) from e
        runtimes[str(alias)] = _build_runtime(str(alias), fields)
    if not runtimes:
        raise SandboxdConfigError(f"{PREFIX}RUNTIMES as JSON names no runtime")
    return runtimes


_NUMERIC_RUNTIME_FIELDS: dict[str, Callable[[str], Any]] = {
    "cpus": _as_float,
    "cpu_shares": _as_int,
    "pids_limit": _as_int,
}


def _build_runtime(alias: str, fields: dict[str, Any]) -> SandboxRuntime:
    """One allowlist entry, with the numeric modifiers converted.

    Values arriving from the compact form are all strings, and `SandboxRuntime`
    wants numbers for three of its fields. Converting here rather than at the
    call sites keeps both forms producing the same object.
    """
    converted = dict(fields)
    for name, parse in _NUMERIC_RUNTIME_FIELDS.items():
        value = converted.get(name)
        if isinstance(value, str):
            converted[name] = _parsed(f"RUNTIMES {alias}.{name}", value, parse)
    try:
        return SandboxRuntime(**converted)
    except TypeError as e:
        raise SandboxdConfigError(
            f"{PREFIX}RUNTIMES entry {alias!r} sets something SandboxRuntime does not have: {e}"
        ) from e
    except ValueError as e:
        raise SandboxdConfigError(f"{PREFIX}RUNTIMES entry {alias!r} is invalid: {e}") from e


__all__ = [
    "PREFIX",
    "SandboxdConfigError",
    "bind_from_env",
    "config_from_env",
]
