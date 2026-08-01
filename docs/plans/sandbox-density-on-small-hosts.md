# Session density on a small host

Research note, August 2026. The question asked was: on one server with **4 GB of
RAM, 200 GB of disk and an ordinary CPU**, how many agent sessions can we carry —
and can we get more by going *below* Docker?

This is a different question from
[Faster sandbox runtimes than Docker](sandbox-runtime-alternatives.md), which
ranked runtimes by cold start and isolation. Under a hard memory ceiling the
ranking changes, and in one place it inverts: **the microVM rungs of that note
are the wrong direction for this box.**

## The answer, up front

Going lower than Docker does not buy density. Three things do, in this order:

1. **Stop paying RAM for idle sessions.** An agent session is idle for the large
   majority of its life — it is a filesystem plus an occasional command, not a
   running program. Today an idle session holds a live container. That is the
   whole problem, and it is the only lever worth weeks of work.
2. **Stop paying the per-container management tax.** The `containerd-shim-runc-v2`
   process behind every Docker container is a Go runtime costing roughly
   [10–20 MB RSS](https://github.com/containerd/containerd/issues/7878) each. At
   100 sessions that is 1–2 GB — half the machine — to supervise processes that
   are doing nothing.
3. **Make every session share one base image**, so the read-only pages of
   libpython, libc and site-packages are cached once for the host rather than
   once per session.

Note what all three have in common: none of them is a runtime swap. The runtime
is not where the memory goes.

## Where 4 GB actually goes

An honest budget for this box, before a single sandbox starts:

| | |
|---|---|
| Kernel, systemd, sshd, journald | 250–400 MB |
| `dockerd` + `containerd` | 120–200 MB |
| `sandboxd` (CPython + FastAPI + docker SDK) | 80–150 MB |
| Page cache and burst headroom you must not spend | 400 MB |
| **Left for sandboxes** | **≈ 2.9–3.1 GB** |

Against that, per session:

| | idle | executing |
|---|---|---|
| shim (`containerd-shim-runc-v2`) | 10–20 MB | 10–20 MB |
| `sleep infinity` + cgroup + veth | 2–4 MB | 2–4 MB |
| whatever the agent left running (REPL, dev server) | 0–60 MB | — |
| `python-analytics` doing real work (DuckDB/Polars) | — | 256–384 MB |
| `python-datascience` doing real work (pandas) | — | 512 MB–1 GB |

Two numbers fall out, and they are an order of magnitude apart:

- **Idle sessions that fit: ~120–200.** Bounded by the shim tax, not by the
  sandbox.
- **Sessions that fit while actually executing: 5–8** on `python-analytics`,
  **2–3** on `python-datascience`.

The gap between those numbers *is* the design. A service that treats "open
session" and "resident session" as the same thing has to size for the second
number and gets 8 users. A service that separates them advertises the first and
schedules against the second.

## Why microVMs lose at this size

The earlier note read Firecracker's "under 5 MiB overhead per microVM" as
comparable density to a container. That figure is the VMM's own bookkeeping. It
is not what a microVM costs:

- **A guest kernel per sandbox.** Every microVM boots its own Linux. Nothing is
  shared with the host or with its neighbours.
- **A guest page cache per sandbox.** The pages of `libpython3.12.so` are cached
  once *inside each VM*. A hundred containers off `python:3.12-slim` share one
  copy; a hundred microVMs hold a hundred copies. On a machine this small that
  single difference dominates every other consideration.
- **Memory is allocated, not borrowed.** [E2B defaults to 1 GB per
  sandbox](https://e2b.dev/docs/sandbox/persistence) for exactly this reason.
  Ballooning claws some back, at latency and complexity.

3 GB divided by a realistic 256 MB Python microVM is **about twelve sandboxes**,
idle or not. Against 120–200 idle containers, that is a 10–15× loss. Firecracker
buys kernel-level isolation, and on this box the price of that isolation is
roughly 90% of the capacity. That is a defensible trade if the threat model
demands it — it is not a density story, and it should never again be written up
as one.

The same argument, weaker, applies to gVisor: a
[130–200 MB per-pod overhead](https://safeguard.sh/resources/blog/container-runtime-runc-vs-crun-vs-gvisor-2026)
plus a syscall penalty costs a third of this host's capacity. Keep it as what
`SandboxRuntime.oci_runtime` already makes it — a per-alias choice for the one
runtime that installs arbitrary packages off the network — not a default.

WebAssembly stays rejected for the reasons the earlier note gives: no `fork`, no
shell, no native wheels. Nothing has changed.

## The levers, ranked by MB recovered per hour of work

### 1. Hibernate idle sessions — the only structural win

Today `Sandboxd.make_room` closes the least recently used idle session and keeps
its workspace. The next request pays a full container create. So eviction is
cheap for the host and expensive for the user, which is why the eviction
threshold has to be generous, which is why idle sessions stay resident.

The container does not have to die to stop costing RAM. `DockerSandbox` already
supports the cheaper move: a **named** container forces `auto_remove=False` and
survives `stop()`, and `_reattach` already restarts one in a
`REATTACHABLE_STATUS`. A stopped container holds no shim, no processes and no
anonymous memory — only disk, of which this box has 200 GB. Restarting one is a
few hundred milliseconds against seconds for a fresh create.

That turns the resident set into a scheduling decision rather than a consequence
of user behaviour:

- **hot** — running, capped at a number the RAM budget can defend (start at 10).
- **warm** — stopped named container plus workspace. Costs disk. Wakes in
  ~200–400 ms.
- **cold** — workspace only, container gone. Costs disk. Wakes in seconds.

Hundreds of open sessions, ten resident. What it does not preserve is process
state: a background server started through `_background.py` dies with the stop.
CRIU (as [zeropod](https://github.com/ctrox/zeropod) uses it — checkpoint to
disk, restore in tens to a few hundred ms) preserves it and is the later, larger
version of this. Start without it; most agent sessions have no process worth
keeping between turns.

### 2. Drop the shim tax

10–20 MB per container to supervise a `sleep infinity` is the largest fixed cost
in the budget after the base OS. Two ways down, both host-level:

- **`crun` instead of `runc`** — C rather than Go, no GC. Reported ~2× faster
  container lifecycle and
  [~30–40% less per-container overhead](https://www.rack2cloud.com/container-runtime-optimization-density-guide/).
  Zero code: a `default-runtime` line in `/etc/docker/daemon.json`. Do this
  first; it is the cheapest MB on the list.
- **Podman rather than Docker** — no daemon, and `conmon` (C, ~1–2 MB) in place
  of the Go shim. Worth 10–18 MB *per session*, which at 100 sessions is over a
  gigabyte. The cost is that `docker-py` talks to Podman's Docker-compatible
  socket well but not perfectly, and it is a real port rather than a config line.
  Measure the shim first; if it lands at the low end this is not worth it.

### 3. Make the base image shared, and lean on compression

- **One base image across runtime aliases.** Page-cache sharing only happens for
  layers that are literally the same. Aliases built from `python:3.12-slim` with
  different package sets share the interpreter; aliases built from different base
  images share nothing. Worth tens of MB per session at this density, for free.
- **zram.** Idle Python heaps compress roughly 3:1. A 1 GB zram swap device on
  this box is worth several hundred MB of effective capacity and costs CPU only
  under pressure. On a 4 GB host this is close to free capacity.
- **KSM** deduplicates identical pages across identical containers. Real savings
  with N copies of the same runtime, but it burns CPU scanning continuously.
  Measure before enabling; on an "ordinary CPU" it may not pay.

## What to change in this codebase

Ordered by ratio of capacity to diff size. **All four have since shipped** — see
the Unreleased section of the changelog; the descriptions are kept as written
because they are the argument, not the record.

1. **`Sandboxd.make_room` should stop, not close.** ✅ Eviction now hibernates:
   the sandbox goes, the session's record, token and event log stay, and the
   next request wakes it. `_Service.hibernate`, and `_Service.sandbox` as the
   wake path.
2. **Split the ceiling in two.** ✅ `max_sessions` is now the resident ceiling
   and `max_open_sessions` the open one, with `evict_idle_after` demoting
   between them. Backpressure applies only when the resident tier is full of
   genuinely working sessions, and waking obeys the same rule.
3. **Let `memswap_limit` be set independently of `mem_limit`.** ✅ Available on
   `DockerSandbox`, `SandboxRuntime` and `SandboxdConfig`. The old behaviour —
   swap pinned to memory — is still the default, because it is right for a disk
   swap and wrong only for zram.
4. **Document the small-host profile.** ✅ `crun` as the daemon default, zram
   plus `memswap_limit`, and one shared base image are now in
   [the remote-sandbox concepts page](../concepts/remote.md#making-it-fast-on-a-small-host),
   alongside the `cpu_shares`-over-`cpus` argument that was already there.
   `runtimes.py` already argues that DuckDB and Polars do the same work in a
   third of the memory; on this box that stops being a preference.

What has *not* shipped is the part that needs measurement first: whether the
shim tax justifies moving off the Docker daemon, and whether wake latency is low
enough to run the resident ceiling aggressively. Both are what the benchmark
below answers.

## The benchmark that settles it

Every number above is someone else's, and the earlier note's second
recommendation — benchmark before building anything — is still unpaid.
`scripts/bench_density.py` takes the measurement on the target host:

```bash
uv run python scripts/bench_density.py --sessions 40 --runtime python-analytics
uv run python scripts/bench_density.py --sessions 40 --hibernate   # wake latency
```

It reports, per session added: host `MemAvailable` delta (the honest number —
it captures shim, veth, cgroup and page cache together), container cgroup usage,
shim RSS, time to first command output, and, with `--hibernate`, stop/start wake
latency. Run it under `runc` and again under `crun` on the same host.

Three numbers decide the plan:

- **Marginal MB per idle session.** Below 8 MB, the shim is not the problem and
  lever 2 can be skipped. Above 15 MB, Podman is worth costing out.
- **Wake latency from stopped.** Under ~500 ms, the hibernate tier is invisible
  to the agent and the hot ceiling can be aggressive. Over ~2 s, it needs CRIU.
- **Where the host degrades.** The point at which `MemAvailable` stops falling
  linearly is the real ceiling, and it will arrive before the arithmetic says.

## Numbers not verified

Everything cited here is vendor documentation or secondary reporting. The
per-microVM figures in particular are argued from how the technology works rather
than measured on a 4 GB host — the conclusion that microVMs lose badly at this
size is robust to the exact numbers, but the 10–15× ratio is not. One claim found
during this research, that OverlayFS achieves approximately zero page-cache
sharing across containers, contradicts how overlayfs reads pass through to the
lower inode; it is not relied on here, and the benchmark measures the effect
directly rather than arguing about it.

## Sources

- [containerd shim memory overhead](https://github.com/containerd/containerd/issues/7878) ·
  [runtime density guide](https://www.rack2cloud.com/container-runtime-optimization-density-guide/)
- [runc vs crun vs gVisor, 2026](https://safeguard.sh/resources/blog/container-runtime-runc-vs-crun-vs-gvisor-2026) ·
  [gVisor performance guide](https://gvisor.dev/docs/architecture_guide/performance/)
- [Firecracker snapshot support](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md) ·
  [E2B sandbox persistence](https://e2b.dev/docs/sandbox/persistence)
- [zeropod — scale-to-zero via CRIU](https://github.com/ctrox/zeropod) ·
  [one year on](https://blog.zwindler.fr/en/2026/05/30/zeropod-v0.12.0-one-year-later-does-scale-to-zero-deliver/)
- [microsandbox / libkrun](https://microsandbox.dev/) ·
  [KSM](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/7/html/virtualization_tuning_and_optimization_guide/chap-ksm)
