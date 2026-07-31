"""Turning a Docker `stats` sample into a `SandboxUsage`."""

from __future__ import annotations

from typing import Any

from pydantic_ai_backends.types import SandboxUsage


def parse_usage(raw: object) -> SandboxUsage | None:
    """Read a non-streaming `container.stats()` payload.

    Returns:
        The usage sample, or `None` when the daemon returned nothing usable.
    """
    if not isinstance(raw, dict):
        return None

    memory = raw.get("memory_stats") or {}
    pids = raw.get("pids_stats") or {}
    return SandboxUsage(
        memory_bytes=as_int(memory.get("usage")),
        memory_limit_bytes=as_int(memory.get("limit")),
        cpu_percent=cpu_percent(raw),
        pids=as_int(pids.get("current")),
    )


def as_int(value: object) -> int | None:
    """Coerce a stats field to `int`, tolerating absent or odd values."""
    return value if isinstance(value, int) else None


def cpu_percent(raw: dict[str, Any]) -> float | None:
    """Derive CPU percent from Docker's two cumulative CPU counters.

    Docker reports totals, not rates, so a percentage only exists relative to
    the previous sample the daemon includes as `precpu_stats`. A container whose
    first sample this is has no delta to divide by, hence `None`.
    """
    cpu = raw.get("cpu_stats") or {}
    precpu = raw.get("precpu_stats") or {}
    usage = as_int((cpu.get("cpu_usage") or {}).get("total_usage"))
    previous_usage = as_int((precpu.get("cpu_usage") or {}).get("total_usage"))
    system = as_int(cpu.get("system_cpu_usage"))
    previous_system = as_int(precpu.get("system_cpu_usage"))
    if usage is None or previous_usage is None or system is None or previous_system is None:
        return None

    system_delta = system - previous_system
    if system_delta <= 0:
        return None

    cores = as_int(cpu.get("online_cpus")) or 1
    return (usage - previous_usage) / system_delta * cores * 100.0
