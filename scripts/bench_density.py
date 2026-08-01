#!/usr/bin/env python
"""Measure what an agent session actually costs on this host.

Answers the three questions in `docs/plans/sandbox-density-on-small-hosts.md`
that no blog post can answer for your machine: how many megabytes each idle
session takes off the host, how long a session waits for its first command, and
how fast a hibernated session wakes.

The load-bearing measurement is the host's `MemAvailable` delta, not the
container's own cgroup usage. Only the host figure counts the supervising shim,
the veth pair, the cgroup structures and the page cache the session brings with
it — which together are most of what an idle session costs.

Linux only, since that is where the number matters and where `/proc` exists.

Usage:
    uv run python scripts/bench_density.py --sessions 40
    uv run python scripts/bench_density.py --sessions 40 --hibernate
    uv run python scripts/bench_density.py --sessions 20 --oci-runtime crun
    uv run python scripts/bench_density.py --sessions 40 --json bench-runc.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic_ai_backends import DockerSandbox

MIB = 1024 * 1024

PROC_MEMINFO = Path("/proc/meminfo")
SHIM_COMMS = ("containerd-shim", "conmon", "crun", "runc")
"""Process names whose resident memory is per-container management overhead."""


@dataclass(frozen=True)
class Sample:
    """One session's cost, measured after it is up and has run a command."""

    index: int
    boot_seconds: float
    """Create, start, and get output back from the first command."""

    host_used_mib: float
    """Fall in host `MemAvailable` attributable to this session."""

    cgroup_mib: float | None
    """What the container itself accounts for, per Docker's stats."""

    shim_mib: float
    """Rise in total resident memory of container-management processes."""

    wake_seconds: float | None
    """Stop, start, and get output back again. `None` without `--hibernate`."""


def mem_available_mib() -> float:
    """Host memory the kernel believes is available, in MiB.

    `MemAvailable` rather than `MemFree` because reclaimable page cache is
    genuinely available, and a benchmark that counted it as used would report a
    full machine after the first image pull.
    """
    for line in PROC_MEMINFO.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    raise RuntimeError("/proc/meminfo has no MemAvailable line")


def management_rss_mib() -> float:
    """Total resident memory of every container-management process, in MiB.

    Scanned by process name rather than by walking a session's children: the
    shim is reparented away from whatever created it, so there is no tree to
    follow. A host running other containers inflates the absolute figure, but
    the benchmark only ever uses differences.
    """
    total_kib = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
            if not comm.startswith(SHIM_COMMS):
                continue
            status = (entry / "status").read_text()
        except OSError:
            # The process exited mid-scan, which for a short-lived `runc` is
            # entirely normal. It contributes nothing either way.
            continue
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                total_kib += int(line.split()[1])
                break
    return total_kib / 1024


def first_command(sandbox: DockerSandbox) -> None:
    """Run the cheapest possible command, and insist it worked.

    Timing `start()` alone would miss the part an agent actually waits for: the
    daemon reports a container running well before an `exec` into it returns.
    """
    result = sandbox.execute("echo ready", timeout=60)
    if "ready" not in result.output:
        raise RuntimeError(f"sandbox did not answer (exit {result.exit_code}): {result.output!r}")


def cgroup_mib(sandbox: DockerSandbox) -> float | None:
    """The container's own memory accounting, in MiB, when the daemon reports it."""
    usage = sandbox.resource_usage()
    if usage is None or usage.memory_bytes is None:
        return None
    return usage.memory_bytes / MIB


def measure_wake(sandbox: DockerSandbox) -> float:
    """Stop the container and bring it back, timing the round trip.

    This is the hibernate tier's whole proposition: a stopped container holds no
    shim, no processes and no anonymous memory, so if this number is small
    enough to hide behind an agent's turn, idle sessions never need to be
    resident.
    """
    sandbox.stop()
    started = time.perf_counter()
    sandbox.start()
    first_command(sandbox)
    return time.perf_counter() - started


def run(args: argparse.Namespace) -> list[Sample]:
    """Add sessions one at a time, measuring the host's response to each."""
    sandboxes: list[DockerSandbox] = []
    samples: list[Sample] = []
    try:
        for index in range(1, args.sessions + 1):
            host_before = mem_available_mib()
            shim_before = management_rss_mib()

            sandbox = DockerSandbox(
                runtime=args.runtime,
                # A named container survives `stop()`, which is what makes the
                # hibernate measurement possible at all.
                container_name=f"{args.prefix}-{index}",
                mem_limit=args.mem_limit,
                cpu_shares=args.cpu_shares,
                oci_runtime=args.oci_runtime,
                network_mode=args.network_mode,
                # `image` has a default, so it is only worth naming when the run
                # is pinned to a ready-made one instead of a built runtime.
                **({"image": args.image} if args.image is not None else {}),
            )

            started = time.perf_counter()
            sandbox.start()
            first_command(sandbox)
            boot = time.perf_counter() - started
            sandboxes.append(sandbox)

            # The daemon writes back cgroup and network state after the exec
            # returns, so an immediate reading understates a session by a few
            # megabytes.
            time.sleep(args.settle)

            sample = Sample(
                index=index,
                boot_seconds=boot,
                host_used_mib=host_before - mem_available_mib(),
                cgroup_mib=cgroup_mib(sandbox),
                shim_mib=management_rss_mib() - shim_before,
                wake_seconds=measure_wake(sandbox) if args.hibernate else None,
            )
            samples.append(sample)
            report_line(sample)
    except KeyboardInterrupt:
        print("\ninterrupted — tearing down", file=sys.stderr)
    finally:
        for sandbox in sandboxes:
            sandbox.stop(remove=True)
    return samples


def report_line(sample: Sample) -> None:
    """One line per session, so a run that degrades shows it while it happens."""
    cgroup = f"{sample.cgroup_mib:6.1f}" if sample.cgroup_mib is not None else "     -"
    wake = f"{sample.wake_seconds:7.2f}s" if sample.wake_seconds is not None else "       -"
    print(
        f"{sample.index:4d}  boot {sample.boot_seconds:6.2f}s  "
        f"host {sample.host_used_mib:7.1f} MiB  cgroup {cgroup} MiB  "
        f"mgmt {sample.shim_mib:6.1f} MiB  wake {wake}",
        flush=True,
    )


def summarise(samples: list[Sample]) -> None:
    """The three numbers the plan turns on."""
    if not samples:
        print("no sessions completed", file=sys.stderr)
        return

    host = [sample.host_used_mib for sample in samples]
    print(f"\nsessions measured        {len(samples)}")
    print(
        f"marginal host MiB/session median {statistics.median(host):.1f}  "
        f"mean {sum(host) / len(host):.1f}"
    )
    print(f"management MiB/session   median {statistics.median(s.shim_mib for s in samples):.1f}")
    print(
        f"time to first command    median {statistics.median(s.boot_seconds for s in samples):.2f}s"
    )

    wakes = [sample.wake_seconds for sample in samples if sample.wake_seconds is not None]
    if wakes:
        print(f"wake from stopped        median {statistics.median(wakes):.2f}s")

    # The arithmetic in the plan assumes every session costs what the median one
    # costs. It stops being true once the host starts reclaiming, and the point
    # where it stops is the real ceiling.
    if len(host) >= 6:
        early = statistics.median(host[: len(host) // 3])
        late = statistics.median(host[-len(host) // 3 :])
        print(f"first third vs last third {early:.1f} MiB -> {late:.1f} MiB per session")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--sessions", type=int, default=20, help="How many sessions to add.")
    parser.add_argument("--runtime", default="python-analytics", help="Built-in runtime name.")
    parser.add_argument("--image", default=None, help="Ready-made image, instead of --runtime.")
    parser.add_argument("--mem-limit", default="384m", help="Per-sandbox memory ceiling.")
    parser.add_argument("--cpu-shares", type=int, default=512, help="Relative CPU weight.")
    parser.add_argument(
        "--oci-runtime", default=None, help="Low-level runtime, e.g. crun or runsc."
    )
    parser.add_argument("--network-mode", default=None, help="Docker network mode, e.g. none.")
    parser.add_argument(
        "--hibernate", action="store_true", help="Also measure stop/start wake latency."
    )
    parser.add_argument(
        "--settle", type=float, default=1.0, help="Seconds to wait before reading memory."
    )
    parser.add_argument("--prefix", default="bench-density", help="Container name prefix.")
    parser.add_argument("--json", type=Path, default=None, help="Write samples here as JSON.")
    args = parser.parse_args()
    if args.image is not None:
        args.runtime = None
    return args


def main() -> int:
    if not PROC_MEMINFO.exists():
        print(
            "This benchmark needs /proc — run it on the Linux host you are sizing.", file=sys.stderr
        )
        return 1

    args = parse_args()
    samples = run(args)
    summarise(samples)
    if args.json is not None:
        args.json.write_text(json.dumps([asdict(sample) for sample in samples], indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
