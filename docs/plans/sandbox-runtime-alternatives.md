# Faster sandbox runtimes than Docker

Research note, July 2026. The question asked was: what open-source runtime is
faster and more efficient than Docker, and lets one host carry more concurrent
sessions?

**The numbers below are from vendor documentation and secondary sources, not from
our own measurements.** They are good enough to rank the options and wrong enough
that no capacity decision should rest on them. The one benchmark that settles it
is at the end.

## The question is two questions

"Faster and more sessions than Docker" splits into problems with opposite
answers:

- **Cold start and density.** What does an idle session cost, and how long does
  an agent wait for its first command? Docker is already good here — milliseconds
  to start, and the cost of an idle container is roughly the cost of its
  processes.
- **Isolation.** Every Docker sandbox shares the host kernel. One kernel
  vulnerability is one escape, and we hand these sandboxes untrusted
  model-written code by design.

The interesting finding is that the second problem has solutions that do not
cost anything on the first. A Firecracker microVM boots in ~125ms with under
5 MiB of VMM overhead, which is *stronger* isolation than Docker at comparable
*startup* cost. The naive assumption — that better isolation means slower — is
wrong at this scale.

> **Correction, August 2026.** An earlier version of that sentence said
> "comparable density", which is wrong. The 5 MiB is the VMM's bookkeeping, not
> a microVM's footprint: each one carries its own guest kernel and its own page
> cache, so it shares nothing with its neighbours where containers share a base
> image. Under a hard memory ceiling microVMs lose badly —
> [Session density on a small host](sandbox-density-on-small-hosts.md) works
> through what that costs on a 4 GB machine. They buy isolation, not density.

So there is no single answer, only a ladder. Each rung costs more to adopt than
the one below it.

## Rung 0 — pass `runtime` through to Docker (hours, no new backend)

`DockerSandbox._run_kwargs` does not set `runtime`, so an operator cannot select
an alternative OCI runtime today. Docker takes one per container, and that single
missing parameter is the difference between "Docker only" and "gVisor, Kata, or
anything else installed on the host":

```python
sandbox = DockerSandbox(image="python:3.12-slim", runtime="runsc")  # gVisor
sandbox = DockerSandbox(image="python:3.12-slim", runtime="kata")  # microVM per container
```

- **gVisor (`runsc`)** intercepts syscalls in userspace. Stronger than a
  container, weaker than a VM, at a stated 10–30% penalty on I/O-heavy work and
  near zero on compute. This is what Modal runs.
- **Kata Containers** puts each container in its own microVM: ~200ms start,
  hardware isolation, OCI-compatible.

Neither is *faster* than plain Docker — gVisor is slower and Kata starts slower.
They buy isolation, not speed. Worth doing anyway because it is a few lines, it
is per-runtime rather than service-wide (`SandboxRuntime` already carries
per-runtime ceilings, so this belongs next to them), and it lets an operator make
the trade without us shipping a backend.

**The actual speed win at this rung is `crun`.** Swapping the host's default
runtime from `runc` (Go, a runtime per container) to `crun` (C, no GC) is a
host-level config change with *zero* code: reported ~21% faster container
lifecycle and around 3 GB of RAM reclaimed per node, which is roughly 20 more
containers on the same box. `youki` (Rust) is in the same class. This costs us
nothing to support — it is a documentation line, not a feature.

## Rung 1 — microsandbox as a second backend (days)

[microsandbox](https://github.com/microsandbox/microsandbox) is the best fit for
this library:

- **libkrun microVMs** — hardware isolation, boot times claimed under 100ms on
  Apple Silicon.
- **Runs standard OCI images**, so our whole `RuntimeConfig` and image-building
  path carries over unchanged. This is the part that matters: a WASM or
  custom-format runtime would throw all of it away.
- **Exposes an HTTP/MCP API for command execution and filesystem access** — which
  is nearly the shape of `SandboxProtocol` already.
- Apache 2.0, works on macOS and Linux.

The mapping onto our protocol is close to mechanical, and `RemoteSandbox` is
already the precedent for "a sandbox that lives in another process". Development
on macOS is a genuine bonus: Firecracker does not run there at all.

[SmolVM](https://github.com/CelestoAI/SmolVM) is the same idea with a different
bet — a single static binary wrapping Firecracker, QEMU *and* libkrun behind one
API, sub-200ms cold, macOS via Hypervisor.framework. Shipped April 2026, so it is
young. Its unified-VMM abstraction is arguably what we would end up writing
ourselves.

## Rung 2 — snapshot and fork: the real density lever (weeks)

Everything above still boots an operating system per session. The technique that
actually changes the ceiling is restoring a pre-booted one.

Firecracker can snapshot a running VM's memory and filesystem and resume it in
**5–30ms** — reported at 49ms p50 in one production system — using `userfaultfd`
for lazy memory loading. The consequence is the important part: you boot Python
once, snapshot after the interpreter and packages are warm, and every subsequent
session restores from that image. Cold start stops being a function of the
runtime and becomes a function of memory paging, and idle sessions can be
snapshotted to disk and evicted at near-zero cost.

That last point is what `evict_idle_after` is groping towards today. Our current
eviction throws the container away and keeps the workspace, so the next request
pays a full start. With snapshots, eviction becomes suspend/resume and the
distinction between "20 open sessions" and "200 mostly-idle sessions" largely
disappears.

- [e2b-dev/infra](https://github.com/e2b-dev/infra) — E2B's self-hostable
  Firecracker orchestration, the reference implementation of this.
- [mitos](https://github.com/mitos-run/mitos) — Firecracker on Kubernetes with
  millisecond memory-snapshot restore *and VM forking* (fork one running VM into
  N copies), declarative CRDs. Forking is interesting for agent work specifically:
  branch a warm sandbox per candidate solution.

## Rung 3 — the thing to know about, not adopt

[OpenSandbox](https://northflank.com/blog/alibaba-opensandbox-architecture-use-cases)
(Alibaba, Apache 2.0, March 2026) is worth reading carefully because **it overlaps
heavily with `sandboxd`**: a Go `execd` injected into each container handling code
execution, file operations and commands, behind a unified API, over both Docker
and Kubernetes runtimes. 7k stars in two days.

That is not a reason to stop — our value is the Pydantic AI integration, the
permission ruleset and the console toolset, none of which it has — but if we are
choosing where to spend effort, "another sandbox service" is now a crowded field
while "the best sandbox *capability* for Pydantic AI agents" is not.

## Rejected: WebAssembly

Wasmtime/WasmEdge/Spin give microsecond cold starts and kilobytes of overhead —
densities nothing here can touch. They are the wrong tool for us anyway:

- No `fork`/`exec`, so no subprocesses, no shell, no `execute()` in any
  recognisable form.
- `pip install` of anything with a native wheel does not work. Our
  `python-datascience` runtime is pandas and numpy; that is the whole point of it.
- Our `BaseSandbox` derives file operations from shell commands.

WASM suits a narrow "evaluate this expression" sandbox. It does not suit an agent
that writes a script, installs a dependency and runs it.

## The constraint that decides everything: `/dev/kvm`

Every microVM option needs hardware virtualization. This matters more for us than
for most, because `sandboxd` exists precisely so the *application* can run in a
container — and a container does not get `/dev/kvm`. Moving to microVMs means the
sandbox host must be bare metal or a VM with nested virtualization:

- **AWS**: previously `.metal` only. Since **16 February 2026** nested
  virtualization is available on C8i/M8i/R8i in all commercial regions, which
  removes the main cost objection.
- **GCP**: nested virt with KVM since 2017, Haswell or later.
- **Azure**: v3 series and newer.

Docker keeps working on anything. That asymmetry is why rung 0 is worth shipping
regardless of whether we ever climb higher.

## Recommendation

1. **Ship the `runtime` passthrough** on `DockerSandbox` and `SandboxRuntime`,
   and document `crun` as a host-level change. Hours of work, no new dependency,
   and it hands operators both the isolation and the density lever without us
   picking one for them.
2. **Benchmark before building anything.** Every number here is someone else's.
   The measurement that decides rungs 1 and 2 is ours to take: on one host, for
   `python-datascience` and a plain shell runtime — time to first command output,
   RSS of an idle session, and how many sessions fit before the host degrades.
   Compare `runc`, `crun`, `runsc` and microsandbox. Our `sandboxd` is already the
   right harness for it, since it reports per-session usage.
3. **Then decide on microsandbox**, as a backend behind the existing protocol
   rather than a replacement for the Docker one.
4. **Treat snapshot/restore as a separate, later question.** It is the largest win
   and the largest change, and it wants `evict_idle_after` to become
   suspend/resume rather than close/reopen.

Do not adopt WASM. Read OpenSandbox before planning the next `sandboxd` feature.
