# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`AsyncBaseSandbox`** — the shell-derived file operations, for a sandbox that is natively asynchronous. `BaseSandbox` gives a subclass every operation for the price of implementing `execute` and `edit`, but only synchronously, so an author reaching a sandbox over asyncssh or an async HTTP SDK got nothing from it and hand-wrote all eight: `ls -la` and its parsing, `awk` for a numbered read, `find -path` for a glob, `grep -rn`, a heredoc write. That is a reimplementation of a class we already ship, and one that drifts from ours the moment either changes. Both bases now derive their operations from one internal module, so a subclass implements `execute` and `edit` — as coroutines — and nothing else.

  Subclassing it rather than wrapping async code in a synchronous facade is not a style question. `ensure_async` cannot see through a facade: it wraps it in a thread adapter, so each call occupies a worker thread that then has to hop back onto the event loop to reach the real async code. A sandbox whose own recovery path also needs a thread — reprovisioning a dead container — waits for a thread that is waiting for the loop, and starves the pool for every other agent sharing it. Reported from a deployment where exactly that froze a whole single-loop runtime rather than one tool call.

- **`is_async_backend`**, and `ensure_async` recognising `AsyncBaseSandbox` by its class. Whether a backend counted as already-async was decided by a single undocumented method-shape check — `read_bytes` being a coroutine function — which third-party authors were reading out of our source and depending on. It also had a hole: the adapter accepts the legacy `_read_bytes` name but the check did not, so a backend async everywhere while spelling it the old way was classified as sync, thread-wrapped, and handed its caller a coroutine object where bytes were expected — no exception, just nonsense. Both names are now checked, the base class is recognised outright, and the contract is documented rather than inferred. It has to stay a shape check rather than `isinstance` against `AsyncBackendProtocol`, because a runtime-checkable `Protocol` compares method *names* and a sync backend has exactly the same ones.

### Fixed

- **A backend's exception no longer ends the agent's run.** The console toolset's `execute` tool caught only `RuntimeError`, and `execute_background` only `RuntimeError` and `PermissionError`. Every bundled backend returns its failures rather than raising, so nothing here exercised it — the cost fell entirely on third-party backends, which arrive with whatever their transport raises: `OSError` from a dropped socket, an SSH or HTTP client's own error class, `TimeoutError`. Any of those escaped the tool and ended the run instead of failing one call. Both now catch `Exception`. The `execute` tool also carried a `# pragma: no cover`, which is why the gap was invisible; it is reachable from the test suite now, like `grep` already was.
- **The shell derivation every sandbox depends on is measured.** `BaseSandbox` carried a blanket `# pragma: no cover`, so the command construction and output parsing behind Docker, Daytona, Kubernetes and any third-party sandbox contributed nothing to the 100% gate. Moving it into one module made it directly testable, and it is now covered — including the quoting of a hostile path, `ls` rows with spaces in the name, a `grep` line containing colons, and the failure branch of every operation.
- **The failure contract is written down.** What a backend must return when an operation fails was real, load-bearing and documented nowhere, so implementors were deducing it from our source. `protocol.py` now states it per method, including the asymmetry that matters most: `read` may return an `Error: ` string but `read_bytes` must return `b""`, because its caller cannot tell an error message from real file content — a probe staging a screenshot would treat `b"Error: not found"` as the image.

- **`SandboxdConfig(container_ttl=...)`** removes a persisted sandbox container that has been stopped for that long, leaving its workspace untouched. It separates the two things a session accumulates: what it *installed* is rebuildable, what it *wrote* is not — so a deployment can reclaim the first on a schedule while keeping an agent's files for ever, which is what its user expects. `workspace_ttl` remains the opposite knob and stays `None` by default. The sweep finds containers by their name prefix rather than from a record of its own, because after a restart Docker is the only source of what is still lying around.
- **`SandboxdConfig(evict_idle_after=...)`** turns the session ceiling from a hard cap on how many sessions may exist into a working-set size. At the ceiling the least recently used session idle for at least that long is closed to make room, instead of the incoming request being refused — which was the wrong answer when the pool was full of sandboxes nobody was using. With `workspace_root` set the evicted session loses nothing but its container: its next request re-attaches and finds its files, for the price of a container start. A session idle for less than the threshold is never a candidate, because killing an agent's work to serve somebody else's first request is worse than making them wait, and a pool of genuinely busy sessions still answers `429`. Requires `workspace_root`, and the config refuses without it rather than silently discarding an evicted session's files.
- **`SandboxdConfig(max_sessions=None)`** removes the session ceiling, for hosts where something else does the bounding. It was typed `int` and so could not be uncapped at all.
- **A `polyglot` runtime**: Python 3.12 and Node 20 in one sandbox with `curl`, `git`, `npm` and `pip` for installing more, plus numpy, DuckDB, Polars and httpx. For the common agent that writes a script, a page and a stylesheet and fetches something — previously every runtime was single-language. Deliberately without pandas: at 97 MB of import it would take a third of a 256 MB sandbox before doing any work. Measured at 1.2 GB built, shared across every session that uses it, and its imports peak at 87 MB so it fits a 256 MB ceiling.
- **`SandboxdConfig(prewarm=True)`** pulls and builds the whole runtime allowlist in the background as the service starts, so the first session on a built runtime no longer pays for the image build — measured at roughly eleven seconds for a pandas runtime — in the middle of a request. Sequential on purpose: several builds at once would fight over the CPU and disk of exactly the small host this is most worth doing on. A runtime that will not build is logged and skipped rather than stopping the rest, and the service answers requests throughout. Only applies to the default Docker builder, since nothing else knows how to warm an injected one.
- **Image builds use BuildKit when the `docker` CLI is available**, which makes package caches survive between builds: editing one package in a runtime re-downloads nothing. The Python SDK has no BuildKit support and its builder rejects `RUN --mount` outright — verified, not assumed — so the generated Dockerfile only carries cache mounts on the BuildKit path and keeps discarding the cache on the classic one. `pip`, `npm`, `apt` and `cargo` each cache the right directory, with `sharing=locked` so two concurrent builds do not corrupt one cache; the apt path also removes Debian's `docker-clean` hook, which would otherwise empty the mount immediately after the install. `GET /policy` reports which builder is in force.
- **`DockerSandbox(tmpfs=...)` and `SandboxdConfig(tmpfs_size="64m")`** give each sandbox an in-memory `/tmp`. Scratch writes previously landed in the container's write layer, which is both slower and the difference between a busy sandbox growing on disk and not. The mount is given `exec` explicitly because Docker mounts a tmpfs `noexec` by default — verified — and that breaks installing any package that builds from source.
- **`DockerSandbox(cpu_shares=...)`, and `cpu_shares` per runtime and service-wide.** A hard `cpus` ceiling means a sandbox cannot use cores that are sitting idle, which on a small host is usually the wrong trade: one agent waits at one core while three are unused. A weight applies only under contention, so a single active sandbox may take the whole machine and several are still divided fairly. The two compose.

- **`DockerSandbox(oci_runtime=...)`, and `oci_runtime` per runtime and service-wide.** Docker hands each container to a low-level OCI runtime and takes one per container, but nothing passed it, so an operator could not choose anything but the daemon's default. It is the only setting that changes how strong a sandbox's isolation is rather than what resources it gets: `"runsc"` (gVisor) moves syscall handling into userspace, `"kata"` gives the container its own kernel in a microVM, and plain `runc` shares the host's — which is what every sandbox has been doing while running untrusted model-written code. Per runtime for the same reason the ceilings are: the runtime allowed to install packages off the network is the one worth paying gVisor's I/O overhead for, and a plain shell is not. `None` by default, because naming a runtime the daemon has not registered makes it refuse the container and turns every session into a `502`. `GET /policy` and the dashboard report the runtime actually in force. `crun` — a drop-in `runc` in C, no different isolation but less overhead per operation — belongs in the daemon's own config and now has a section in the installation docs, along with the caveat that its widely quoted memory figure is a Kubernetes/CRI-O measurement that does not transfer to Docker unchanged.

### Fixed

- **`container_ttl` now actually reclaims anything.** The sweep was written, tested and reported by `GET /policy` — and the dashboard rendered it as "reclaimed *n* after stop" — but nothing outside the tests ever called it: the periodic loop only swept workspaces. An operator setting `persist_containers=True, container_ttl=3600` was told their builds were being reclaimed while stopped `sandboxd-*` containers accumulated until a session was closed with `purge`. The loop now runs both passes, and runs them on the worker pool, because deleting a directory tree and asking the daemon to list and remove containers both block. A service given an injected `sandbox_builder` is not asked for a daemon it may not have.
- **A malformed token is a `401`, not an unauthenticated `500`.** Header values reach a handler latin-1 decoded, so a client is free to send a byte above 127 — and `secrets.compare_digest` refuses a non-ASCII `str` outright rather than returning `False`. One such byte in `X-Sandbox-Token` therefore raised `TypeError` out of the dependency on **every** authenticated endpoint, before any authorization ran. Tokens are now compared as bytes. The line was covered; the input class was not.
- **Inspecting a session reaped mid-request no longer returns `500`.** `describe` read its record out of the service's own dict having trusted the authorization check — but `GET /sessions/{id}?usage=true` awaits a Docker stats call in between, which takes 1–2 seconds, and the idle reaper runs on its own timer. The record could be gone by the time it was read, raising `KeyError`. It is looked up again and reported as `404`, which is what actually happened. A listing takes one such sample per sandbox, so it now drops the vanished row instead of failing the operator's whole view for one reaped session.
- **Starting and stopping a sandbox no longer blocks the event loop.** `SessionManager.get_or_create` called `sandbox.start()` inline, which for a cold runtime pulls or builds an image — seconds to minutes, during which no other session's command could run, on a service whose entire purpose is serving several tenants at once. Both lifecycle calls now run on a thread pool via the new `SessionManager(executor=...)`, which `sandboxd` points at its own worker pool rather than asyncio's shared default one. `DELETE /sessions/{id}?purge=true` likewise stopped discarding a container and deleting a directory tree on the loop.
- **Two requests naming one session id no longer both get it.** With the container start now suspending, both could pass the "is this id taken?" check, both were handed the same sandbox, and the second overwrote the first's record with a freshly minted token — silently invalidating the token the first caller was holding, including for its own `stop()`, and never producing the documented `409`. The id is claimed before the first await, so the loser is told. The per-tenant ceiling counts opens still in flight for the same reason: counting only registered sessions let a concurrent burst walk straight past a limit none of them had registered against yet.
- **One dangling symlink no longer breaks a whole archive listing.** `ln -s missing.txt report.md` inside a sandbox resolves to a path *inside* the workspace, so containment passed and only the `stat` failed — turning the directory's listing into a `404` whose message additionally carried the resolved host path. Untrusted code in the container can plant one. Such an entry is now omitted, exactly as one pointing out of the workspace already was.
- **`RemoteSandbox` degrades on a success status carrying the wrong body.** The class promises that failures are returned and never raised, but every operation called `model_validate(response.json())` unguarded, and `_ensure_session` caught only `RuntimeError` — so an auth proxy or captive portal answering `200 text/html` in front of the service raised out of a tool call and ended the agent run. Every parse now goes through one helper that treats an unusable body as a failed operation. A `session_id` or `tenant` the service would reject is validated when the sandbox is constructed rather than out of whichever tool call happens to open the session.
- **The workspace sweep survives a directory vanishing under it.** `is_dir()` followed by `stat()` leaves a window in which a concurrent `?purge=true` deletes the directory between the two calls — reachable now that both run on the same worker pool rather than being serialised by the event loop — and the unguarded second call aborted the whole pass over one entry. One guarded `stat` does both jobs.
- **`DockerSandbox` is actually measured.** The class carried a blanket `# pragma: no cover`, so the 272 statements that hold the Docker socket — `read`, `write`, `edit`, `execute`, container creation and reattachment — contributed nothing to the 100% gate: they were exercised only by `@pytest.mark.docker` tests, which CI deselects. The pragma is gone and those paths are now covered against a fake daemon. Writing the fake surfaced that `get_archive` must return a *generator* rather than any iterator, which is exactly what the early `close()` on an oversized file depends on. `"pass"` is likewise gone from the coverage exclusions, where it was hiding any `except: pass` from the report.
- **Shutdown stops sessions concurrently.** Each stop waits for the process inside to die, and doing that one session after another turned a full pool's teardown into minutes — long enough for an orchestrator to lose patience and kill the process halfway. One uncooperative sandbox no longer strands the rest either; it is logged and the others still stop.
- **A stopped `RemoteSandbox` is usable again.** `stop()` closes the HTTP client it owns, and every later operation then reported the service as unreachable — for ever, and untruthfully. The client is rebuilt on the next `start()`, which is the behaviour `DockerSandbox` already had: stopping it does not retire the object, it just ends the session. A client supplied by the caller is still never closed.
- **The dashboard's markup is read from disk once** rather than on every request to `/ui`.
- **`ReadRequest.offset` and `limit` are bounded.** A negative offset was not rejected and did not fail; it sliced from the end of the file and quietly returned the wrong lines.

- **Eviction can no longer interrupt a running command.** A candidate was chosen by `last_activity`, which is stamped when a command *begins* — so a command still running after a minute looked a minute idle and could be evicted mid-flight, killing an agent's work to serve somebody else's first request. The service now counts operations in flight per session, through the same wrapper that writes the activity log, and a session with any is never a candidate. That is also what makes `evict_idle_after=0` meaningful: "evict anything not actually running something" rather than "evict anything that started something a while ago".
- **`GET /sessions?usage=true` no longer blocks the whole service.** One Docker stats call takes **1–2 seconds** — the endpoint waits for a second sample before it can report a CPU rate — and the listing made one per sandbox, sequentially, *on the event loop*. With twelve sessions that is a twelve-to-twenty-second request during which no agent's command could run, and the dashboard polls it every three seconds with sampling on by default. Samples are now taken concurrently on the service's worker pool and cached for `USAGE_CACHE_SECONDS`, so a poll costs one round trip's latency regardless of session count and repeated polls cost nothing. `describe` takes an already-taken sample rather than deciding to fetch one, because the decision belongs where the concurrency is.
- **A runtime edit no longer orphans its previous image for ever.** `image_tag_for` embeds a digest of the `RuntimeConfig`, so changing one package mints a new tag and left the old image on disk with nothing to reclaim it — a few hundred megabytes per edit, accumulating silently, and the same shape of leak as the workspaces that `workspace_ttl` now sweeps. A build prunes the images it supersedes, skipping any still backing a running container, since a session opened before the edit is legitimately using it.
- **A runtime allowlist entry is now a `SandboxRuntime`, with its own ceilings.** The allowlist mapped an alias to a bare image string, so every sandbox on a service ran under one memory and CPU limit — and one number is wrong for a whole service: a notebook-style data runtime needs several gigabytes where a plain shell needs a few hundred megabytes, and forcing one value on both either starves the first or over-commits the host for the second. An entry now carries `mem_limit`, `cpus`, `pids_limit` and `network_mode` of its own, so one runtime can be given four gigabytes and another can be the only one allowed to reach the network. A ceiling left unset takes the service-wide value, which is a *default rather than a maximum* — what bounds the host is `max_sessions` times the largest runtime ceiling. A bare string still works wherever an entry is expected. **The client's side of this is unchanged: a request still names an alias and nothing else.**
  - An entry may also name a `RuntimeConfig` (or a built-in runtime by name) instead of a ready-made image, so `sandboxd` can serve environments whose packages are *built* into an image on first use. Until now it could only run images that already existed. The service's `work_dir` is forced onto a built runtime, because the workspace volume, the archive endpoints and a client's paths must not end up disagreeing about where files live.
  - `SandboxBuilder` therefore receives `(session_id, SandboxRuntime)` rather than `(session_id, image)`, and `SandboxdConfig.resolve_image` is now `resolve_runtime`. `ServicePolicy.runtimes` is a list of `RuntimePolicy` carrying each alias's *effective* ceilings, because the number an operator needs is the one actually in force, not the one before the override.
  - **`DEFAULT_RUNTIMES`** is what a service allows when its operator names nothing — `python` and `node`, both ready-made and without ceilings of their own, because a default that built package sets would make a fresh deployment's first session take minutes. **`SUGGESTED_RUNTIMES`** is a fuller catalogue to adopt or copy from, opt-in because every entry is a commitment on the operator's own host.
- **`SUGGESTED_RUNTIMES` sizes the analytics runtime for what it actually needs**, and says why in the config. Measured on one 188 MB CSV with the same `GROUP BY`: pandas peaks at 570 MB and is killed outright under a 384 MB ceiling; DuckDB answers in 312 MB and Polars survives the ceiling too, both roughly eight times faster. So `python-analytics` now carries a 1 GB ceiling rather than 4 GB, which roughly doubles how many analysis sandboxes fit in a fixed amount of RAM, while `python-datascience` keeps its 4 GB because that is pandas' appetite rather than the task's. Both stay in the catalogue — most model-written code reaches for pandas first — but the descriptions now steer the choice.
- **Eight more built-in runtimes** (`BUILTIN_RUNTIMES` goes from 5 to 13): `python-analytics` (DuckDB, Polars, PyArrow), `python-scraping`, `python-documents`, `node-typescript`, `bun`, `deno`, `go` and `rust`. Each answers a task agents are actually given; the four new ready-made images add no build step at all.
- **A remote sandbox opens its session on the first operation, not on construction.** Building a `RemoteSandbox` now performs no I/O at all, so an agent granted a sandbox it never uses costs no session, no container and not even a round trip — which is what makes it reasonable to grant the capability to an agent that only *might* need files. Opening is guarded, so two operations arriving together on the adapter's thread pool open one session rather than racing each other into a `409`, and a failed open degrades like any other operation instead of ending the run. `start()` remains, for pre-warming a sandbox before a latency-sensitive turn.
- **`GET`-free workspace browsing: `POST /workspaces/{id}/ls` and `/read`, with the `WorkspaceArchive` client.** These read the service's host volume directly, so listing the files a conversation produced last week costs no container start and works for a session reaped long ago — previously the only way to see them was through a live sandbox, which meant booting a container and reaping it again minutes later. Service token only: a reaped session has no token of its own left, and the intended caller is an application proxying file views to its users after applying its own authorization. Unlike `RemoteSandbox` these raise rather than degrade, because no model is waiting on them and an application answering "show me my files" must be able to tell "there are none" from "the service is misconfigured"; `WorkspaceArchiveError.status_code` carries what the service said.
  - Reading the host filesystem makes path containment the whole game, so both ways in are closed by resolving first and checking after: `..` in a requested path, and **a symlink the sandbox itself planted** — untrusted code in a container can run `ln -s /etc/shadow notes.txt`, which points at the container's own file from inside but would resolve to the host's when followed from outside. A path resolving outside the workspace is refused even when the link sits inside it, and such an entry is omitted from a listing rather than reported.
  - Paths are relative to the sandbox's work directory, and an absolute in-container path resolves to the same file, so a UI can hand back exactly the path a live listing showed it. Files the agent wrote outside the work directory are not on the volume, and so not in the archive.
- **`SandboxdConfig(persist_containers=True)`** gives each sandbox a container name derived from its session, so a reaped session is *stopped* rather than discarded and the next attach restarts the same filesystem. `workspace_root` alone preserves only the work directory — `pip install` and `apt-get install` write outside it, so an agent working in what is meant to be "the same machine" reinstalled its dependencies after every idle timeout. Off by default, because stopped containers then accumulate until a session is purged; and a service setting rather than a request field, because an agent author choosing this would be choosing the host's disk consumption.
- **`DELETE /sessions/{id}?purge=true`** and **`RemoteSandbox.stop(purge=True)`** discard the session's container and its host workspace, for when the thing it belonged to is gone — a deleted conversation, a departed user. A plain close still keeps both, which is what lets a later attach find the same files.
- **`SandboxdConfig(workspace_ttl=...)`** sweeps workspace directories no session has opened for that long, on the same interval as idle reaping. Without it, every workspace ever created stayed on disk for the life of the deployment: `workspace_root` had no reclamation of any kind, and nothing but an operator's cron job would have removed a workspace belonging to a conversation deleted months ago. The clock runs from when a session was last *opened*, so a long-running session is never swept for being old.
- **`SandboxdConfig(max_sessions_per_tenant=...)`** with `CreateSessionRequest.tenant` (and `RemoteSandbox(tenant=...)`). `max_sessions` alone caps the service, which is no help when one application serves many tenants — the busiest one fills the pool and every other tenant gets `429`. The label is declared by the client rather than parsed out of the session id, so the service imposes no id convention; it is capacity accounting that grants and authorizes nothing, since only a holder of the service token can open a session at all. Reported back in `GET /sessions` and `GET /policy`.
- **A sandbox may now outlive the run that created it.** `CreateSessionRequest.reuse` (and `RemoteSandbox(reuse=True)`) attaches to the session already open under a `session_id` instead of failing, which is what makes a per-conversation sandbox possible at all: a second `RemoteSandbox` naming an open id previously got `409`, and `start()` turns any 4xx into `RuntimeError`. The attaching caller is handed the token the session already has, rather than a fresh one that would cut off whoever holds it. A `runtime` disagreeing with the open session is refused — honouring it would mean replacing a live sandbox and discarding the files the caller came back for.
- **`SandboxdConfig(workspace_root=...)`** mounts `{workspace_root}/{session_id}/workspace` into each sandbox, so a session's files survive its container. Without it they live only in the container's write layer, and an idle reaping between two turns of one conversation discards them silently. Session ids are pattern-checked before they reach a path, so one cannot traverse out of the root.
- **`SessionManager(on_release=...)`** fires with a session id just after its sandbox is stopped, by `release` and therefore by idle cleanup too. For a caller keeping per-session state of its own, this is the only notice a reaping happened — polling `sessions` would mean discovering it late, or never.
- **`create_console_toolset(backend=...)` and `ConsoleCapability(backend=...)`.** The console tools read `ctx.deps.backend` through the `ConsoleDeps` protocol, which no host owning its own deps type can satisfy — a platform that assembles agents from configuration has its own deps class and should not grow a `backend` field for one capability. With an explicit backend the capability carries it and the deps type stops mattering, which is also what lets a single agent hold one particular sandbox.
- **Remote sandboxes over HTTP** (`src/pydantic_ai_backends/remote/`). An application can now use sandboxes that live in another process, so it never needs Docker access itself — the point being that a containerised app which mounted `/var/run/docker.sock` to start sandboxes would be handing itself host root, and nesting Docker in Docker to avoid that is worse.
  - `remote/wire.py` — the HTTP contract as Pydantic models, one source of truth for both sides. The operation endpoints keep the field names `KubernetesPodSandbox(mode="http")` already sends (`/exec`, `/read`, `/write`, `/ls`, `/glob`), which until now existed only as hand-built `json={...}` dicts; `/edit`, `/grep`, `/exists` and `/read_bytes` are additions. Binary payloads travel base64-encoded, so a non-UTF-8 file survives a round trip (the existing `/read`-based `read_bytes` could not).
  - `RemoteSandbox` — client implementing the same synchronous surface as `DockerSandbox`, so it drops into `SessionManager` or a console toolset unchanged. Operations degrade rather than raise on transport failure, matching `LocalBackend`/`DockerSandbox`; only `start()` raises, because a caller that cannot get a sandbox at all needs to know why. Needs the new `remote` extra (`httpx`).
  - `sandboxd` (`remote/server.py`, new `server` extra) — a FastAPI service owning Docker, with session lifecycle, pooling, idle reaping, capacity backpressure (`429`), `GET /sessions` with optional `docker stats` sampling, per-session inspection and an unauthenticated `/healthz`. Blocking sandbox calls run on the service's own thread pool rather than asyncio's shared default one.
  - **Security model:** clients choose nothing about the container. Image, mounts, network mode and every resource ceiling come from `SandboxdConfig`; a request carries at most a runtime *alias* validated against a server-side allowlist. Session ids are pattern-checked so they cannot traverse a directory. Each session gets its own token, and the token is verified before existence is revealed so an unauthenticated caller cannot enumerate session ids by watching 401 turn into 404.
- **The `sandboxd` dashboard is now three views rather than one crowded page.** Everything shared one screen, which left the session detail — terminal included — squeezed into a narrow column beside a table it was competing with for width, while the page below it sat empty.
  - **Sessions** carries capacity and the table at full width, now showing each session's `tenant`, and can open a session with a runtime, an id and a tenant.
  - **Workspace** is one session at full width: a terminal about three times its old size with history recall and a clear action, a two-pane file browser that puts the preview beside the listing, the activity log and an info panel.
  - **Runtimes & policy** shows the allowlist as cards — image, description, memory, CPU, processes, network, and whether the first session builds an image — beside the service defaults and the retention settings. The new-session form names the ceilings of whichever runtime is picked, so an operator is not guessing what a choice costs.
  - The file browser reads the live sandbox by default and can switch to the **stored workspace**, which is served from the host volume: reading a *stopped* session live restarts its container, and the archive needs no container at all. The distinction is stated in the pane rather than left to be discovered.
  - Rewritten against the house CSS and HTML standards — oklch tokens driven by one hue, cascade layers, logical properties, a container query for the file split, `:focus-visible` rings, a `prefers-reduced-motion` guard, real `<button>`s for every control and complete `role="tab"`/`aria-selected`/`aria-controls` wiring on both tab strips. Still one self-contained file with no build step and no CDN, and `tests/test_ui.py` now pins that, along with every element lookup in the script resolving and every tab having a panel.
- **An optional dashboard for `sandboxd`** at `/ui`, enabled with `SandboxdConfig(ui_enabled=True)` (off by default). One self-contained HTML file — no build step, no npm, no CDN — served straight from the package, so it works offline and behind a strict CSP. Lists live sessions with idle time and memory against each sandbox's ceiling, shows the policy in force, and can open, inspect and terminate sessions. Each session gets a workspace view with four tabs:
  - **Terminal** — scrollback, colour-coded exit codes, and `↑`/`↓` command-history recall. Not a PTY: the protocol is request/response, so this runs one command per submission rather than holding an interactive shell.
  - **Files** — breadcrumb navigation over `/ls` with a file preview via `/read`.
  - **Activity** — the session's operation log, so an operator can watch what an *agent* is doing rather than only their own clicks.
  - **Info** — created/idle timestamps and sampled resource usage.

  The token is held in `sessionStorage` rather than `localStorage`, since a root-equivalent credential should not outlive the browser session. The HTTP API is unchanged whether or not the UI is on; the page is static and every call it makes is authenticated exactly like any other client's.
- **A per-session activity log**, exposed at `GET /sessions/{id}/events?after=<seq>` for incremental polling. Every file and command operation records what was addressed, whether it succeeded, a short outcome summary and a duration — recorded even when the operation raises, which is the case an operator most wants to see. Payloads are deliberately never stored: an audit trail holding file contents or command output would be a data leak that also grows without bound. The log is a bounded ring buffer per session (200 entries) and targets are truncated, so a long-lived session cannot grow the service's memory. Authorization matches the operation endpoints, so a session token reads only its own log.
- **`GET /` now describes the service** instead of returning a bare 404, listing the mounted endpoints (derived from the app, so the list cannot go stale), the docs URL and the dashboard URL when enabled.
- **`GET /policy`** (service token) reports the ceilings and image allowlist actually in force, so an operator can read the limits off the running service rather than inferring them from a config file.
- **`DockerSandbox.resource_usage()` and the `SandboxUsage` type** (`sandbox.py`, `types.py`). Samples memory, CPU percent and process count from a single non-streaming `stats()` call, so a session can be inspected without reaching into the container object. CPU is `None` when the daemon reports no previous sample to compute a rate against.

- **`DockerSandbox` resource limits** (`src/pydantic_ai_backends/backends/docker/sandbox.py`). Containers previously ran with no ceiling of any kind, so a single agent could exhaust the host:
  - `mem_limit` (Docker syntax, e.g. `"512m"`) also pins `memswap_limit` to the same value — without a matching swap ceiling the kernel lets a container over its memory limit swap instead, starving the host.
  - `cpus` (in cores, e.g. `1.5`) maps to `nano_cpus`.
  - `pids_limit` defaults to `512`, bounding a runaway `fork` loop. Pass `None` to disable. Note that once a container hits the ceiling its processes stay alive, so further commands fail until they exit.
  - `security_opt=["no-new-privileges:true"]` is now always applied, denying sandboxed code the one cheap escalation route a container leaves open. Verified not to affect `pip`.
  - `max_read_bytes` (default 8 MiB) caps what `read`/`read_bytes`/`edit` will pull out of a container.

- **`SessionManager(max_sessions=...)`** (`src/pydantic_ai_backends/backends/docker/session.py`). Once the ceiling is reached, `get_or_create` raises the new `SessionLimitExceeded` for new session ids instead of starting an unbounded number of containers. Existing live sessions are still served at the cap. Uncapped by default.
- **`AsyncBackendAdapter(..., executor=...)`** and `ensure_async(..., executor=...)` (`src/pydantic_ai_backends/adapter.py`). Blocking backend calls previously always went to asyncio's default thread pool, which holds `min(32, cpu_count + 4)` workers (14 on a 10-core host) and is shared with everything else in the process — so a handful of concurrent `npm install`-length commands filled it and unrelated reads and writes queued behind them. Passing a dedicated pool isolates sandbox work. `ensure_async` is idempotent on adapters, so wrapping once and passing the adapter around makes one pool serve every call site.
- **`DockerSandbox.stop(remove=True)`** (`sandbox.py`) deletes the container and its write layer. Named containers deliberately survive a plain `stop()` — reuse across restarts is the point of `container_name` — but until now nothing could remove one at all.

### Changed

- **Internal restructuring for readability, no change to the public API.** Every name in `pydantic_ai_backends.__all__` still imports from the same place; what moved is private. The library grew by accretion — five backends each carrying their own copy of path normalisation, string replacement, text decoding and output caps, with modules reaching into each other's underscore-prefixed helpers. That duplication is now shared:
  - New private modules hold what several backends need: `_editing.py` (the `edit` replacement rules and their wording), `_paths.py` (virtual path normalisation and validation), `_text.py` (encoding detection, decoding and PDF extraction, lifted out of `DockerSandbox` methods), `_limits.py` (the output and read ceilings that four modules each defined separately), `_optional.py` (every optional-extra import, so a missing dependency always names the extra that provides it).
  - `backends/docker/sandbox.py` is down from 1071 to ~540 lines: the shared Docker client moved to `_client.py`, Dockerfile generation and image resolution to `_image.py`, and `stats()` parsing to `_stats.py`. `backends/base.py` no longer exports lazy-import helpers or extension sets for other modules to import through it.
  - `LocalBackend` composes rather than accumulates: the background-process registry is now `backends/_background.py` and the synchronous permission logic — including the "ask with nobody to ask" reconciliation — is `backends/_guard.py`. This drops the reach into `PermissionChecker._find_matching_rule` from another module.
  - `toolsets/console.py` is down from 1082 to ~630 lines, with tool text in `descriptions.py`, image/document handling in `_content.py`, read-fingerprint tracking in `_tracking.py` and ruleset interpretation in `_ruleset.py`.
  - `CompositeBackend` and `AsyncCompositeBackend` share one `PrefixRouter` instead of two copies of the routing and root-aggregation logic.
  - Private helpers that other modules imported are now public where they belong (`glob_to_regex`, `matches_pattern`, `PermissionChecker.find_matching_rule`, `is_ignored_path`, `shell_argv`, `deny_rules`), and comments that restated the code were deleted in favour of names, types and docstrings that carry the meaning. Rationale comments were kept only where they explain a decision the code cannot.
- **`SessionManager` reads a documented sandbox surface instead of private attributes.** `BaseSandbox` now exposes `last_activity` and `touch()`, and `DockerSandbox` exposes `idle_timeout`; the manager prefers these and still falls back to `_last_activity` / `_idle_timeout`, so custom `sandbox_factory` sandboxes written against the old contract keep working.
- **Every backend's `edit` now reports the same two failures the same way**: `String '<old>' not found in file` and `String '<old>' found N times. Use replace_all=True to replace all, or provide more context.` `DockerSandbox`, `DaytonaSandbox` and `KubernetesPodSandbox` previously omitted the string, and Kubernetes worded the second case differently.
- **Glob patterns in permission rules are compiled once and cached.** Every `check_sync` walked a ruleset's rules and rebuilt a regex for each one, so a read against `DEFAULT_RULESET` recompiled twelve patterns.
- **`create_ruleset(default=...)` is typed `PermissionAction`** rather than `str`, which removed eight `# type: ignore[arg-type]` suppressions from `presets.py`.
- **Idle sandbox cleanup no longer dies on the first failure** (`session.py`). `start_cleanup_loop`'s body had no exception guard, so one raise — an unreachable daemon, or a custom-factory sandbox missing the private `_last_activity` stamp — killed reaping permanently and near-silently, leaving every later container to accumulate. Failures are now logged and retried on the next tick; cancellation still propagates.
- **`cleanup_idle` honours each sandbox's own `idle_timeout`** (`session.py`), falling back to the manager's `default_idle_timeout`. `DockerSandbox` has always accepted and documented `idle_timeout` but nothing ever read it. An explicit `cleanup_idle(max_idle=...)` still overrides every sandbox.
- **`cleanup_idle` tolerates sandboxes without an activity stamp** (`session.py`) instead of raising `AttributeError` on the private attribute of a duck-typed object; such sandboxes are simply never reaped.
- **`is_alive()` caches the daemon's answer for 5 seconds** (`sandbox.py`). It does a `reload()` round trip and `SessionManager.get_or_create` calls it on every request, so each agent turn was billed a round trip just to confirm liveness.
- **A sandbox that fails during `start()` is stopped and not registered** (`session.py`). It was previously dropped on the floor while possibly holding a created container that nothing would ever clean up. Interned `asyncio.Lock` entries for sessions that were rejected or failed to start are now pruned during `cleanup_idle`, rather than accumulating one per failure.
- **One Docker client per process instead of one per sandbox** (`sandbox.py`). `_ensure_container()` called `docker.from_env()` for every sandbox; that negotiates the API version with a blocking `GET /version` (~8.5 ms measured) and builds a `requests.Session` with its own connection pool which was never closed, so every session pinned a socket pool for as long as its container object lived. The client is now shared, and rebuilt after a fork — its pooled sockets must not be used from two processes at once, and web/task servers routinely fork workers after import.
- **Encoding detection now samples a 32 KiB prefix** (`sandbox.py`). `chardet` is pure Python and linear in input size, so it ran over whole files: detection on a 4.4 MB file took **7.2 s** and now takes **54 ms** (135× faster) with the same verdict. Files are still decoded in full, and the binary-file heuristic still inspects the whole text.
- **Oversized files are refused instead of buffered into host memory** (`sandbox.py`). `read_bytes` concatenated the tar stream and then copied it through a second `BytesIO`, holding several copies of the file at once, with no upper bound — reading 20 lines of a 500 MB log transferred all 500 MB. The payload now accumulates directly into the buffer `tarfile` reads from, and a file over `max_read_bytes` is rejected using the size Docker reports in a response header, before any content crosses the socket. `read` and `edit` report the limit and suggest reading a slice with `execute()`; `read_bytes` keeps its documented empty-bytes contract.
- **`execute()` discards output beyond the cap before decoding** (`sandbox.py`), rather than decoding the whole payload and then truncating it — this doubled peak memory on commands like `cat big.log`. The 100 000 cap is now measured in bytes rather than characters.
- `_build_runtime_image()` passes `usedforsecurity=False` to `hashlib.md5()`; the digest only tags a cache image, and plain `md5()` is unavailable on FIPS-enforcing hosts.

### Fixed

- **A session reaped for idleness no longer leaks its bookkeeping** (`remote/server.py`). `SessionManager.cleanup_idle` dropped the sandbox but nothing told the service, so the `_Session` record — its token and its whole 200-entry event log — stayed for the life of the process, `GET /sessions/{id}` answered from a record with no sandbox under it, and the new `reuse` path would have attached to one. The service now registers `on_release` and forgets the session with it.
- **`ConsoleCapability(edit_format="hashline")` now registers `hashline_edit`.** The format only ever reached `get_instructions()`, so the injected prompt told the model to call `hashline_edit` while the toolset registered `edit_file` — an agent configured for hashline editing could not edit anything.
- **`StateBackend.read_bytes` returns `b""` for an unsafe path** instead of the error message encoded as bytes, which a caller could not tell from real file content beginning with `Error:`. This matches `LocalBackend` and the documented contract.
- **`KubernetesPodSandbox.edit` no longer carries a dead branch** checking `read_bytes` for a `b"[Error: ...]"` sentinel. `BaseSandbox.read_bytes` has returned `b""` on failure for some time, so the sentinel could not occur and the guard only pinned behaviour that reality never produced.
- **`backends/kubernetes.py` no longer carries `# noqa: WPS433` directives** for a rule this project does not configure (`wemake-python-styleguide`), which made `ruff check` fail with five `RUF100` errors on a clean checkout.
- **The Kubernetes tests no longer leave a fake `httpx` in `sys.modules`** (`tests/test_kubernetes_sandbox.py`). The stub was installed at module import time and never removed, so every test module collected afterwards saw the fake instead of the real library. `KubernetesPodSandbox` imports `httpx` lazily, so the stub is now scoped to that module with a fixture.

## [0.2.16] - 2026-07-18

### Fixed

- **Permission rules are now enforced on every content-returning path of `LocalBackend`** (closes [#62](https://github.com/vstorm-co/pydantic-ai-backend/issues/62)) (`src/pydantic_ai_backends/backends/local.py`). Previously only `read`/`write`/`edit` and the execute command-pattern check consulted the ruleset, so a `deny` rule like `**/restricted/**` could be bypassed:
  - `read_bytes` now applies the same "read" rules as `read` (a denied path returns `b""`) — this also closes the leak through the console toolset's `read_file` on images/documents, which reads via `read_bytes`.
  - `grep_raw` no longer returns matches from files denied for "grep" **or "read"** (grep leaks content, so read denies must apply), and an explicit "grep" deny on the search path errors the search.
  - `ls_info` / `glob_info` hide entries and matches with an explicit "ls" / "glob" deny. Listings can't prompt, so "ask" is treated as visible — a ruleset whose global default is "ask" keeps listing as before.
  - `execute` / `async_execute` / `execute_background` gain a best-effort path guard: path-looking tokens in the command are resolved against the backend root and denied when they hit a "read"/"write" deny rule, catching the straightforward `cat restricted/secret.txt` bypass. Documented explicitly as defense-in-depth, not a security boundary — use `DockerSandbox` (or an execute default of "deny"/"ask") for enforced isolation. The permissions docs gained a section spelling out these semantics.

## [0.2.15] - 2026-06-27

### Added

- **`AsyncCompositeBackend` — async path-prefix routing across mixed sync/async backends** ([#57](https://github.com/vstorm-co/pydantic-ai-backend/pull/57), extends [#55](https://github.com/vstorm-co/pydantic-ai-backend/pull/55)) (`src/pydantic_ai_backends/backends/composite.py`). An async counterpart to `CompositeBackend`: routes file operations to sub-backends by path prefix (longest match wins), wrapping sync sub-backends via `ensure_async()` internally, and aggregating root `ls_info`/`glob_info`/`grep_raw` across routes. Constructor accepts `BackendProtocol | AsyncBackendProtocol` for both `default` and `routes`. Exported from the package root.
- **Background (long-lived) process support for `LocalBackend`** ([#58](https://github.com/vstorm-co/pydantic-ai-backend/pull/58)) (`src/pydantic_ai_backends/backends/local.py`, `src/pydantic_ai_backends/protocol.py`, `src/pydantic_ai_backends/types.py`). Lets dev servers, watchers, and other long-running commands outlive a single `execute()` call (which kills its whole process tree on timeout):
  - `LocalBackend.execute_background()` / `read_background()` / `kill_background()` / `list_background()` / `kill_all_background()` — spawn a detached process (`start_new_session=True`), spool stdout/stderr to temp files, drain incrementally by byte offset, and tear down whole process groups with `killpg`.
  - New runtime-checkable `BackgroundSandboxProtocol` and `AsyncBackgroundSandboxProtocol` (extending the existing sandbox protocols, which are left untouched), plus `AsyncBackgroundSandboxAdapter` and `ensure_async()` routing to it.
  - New console tools `run_in_background` / `read_output` / `kill_shell` / `list_shells`, gated behind `include_background` (default `True`) and `include_execute`.
  - New `BackgroundHandle` / `BackgroundOutput` / `BackgroundProcessInfo` dataclasses, exported from the package root.
- **Image downscaling on read** ([#58](https://github.com/vstorm-co/pydantic-ai-backend/pull/58)) (`src/pydantic_ai_backends/toolsets/console.py`; optional `images` extra). `read_file` resizes images whose longest edge exceeds 1568px (aspect preserved, re-encoded) before returning `BinaryContent`, so large screenshots don't waste tokens or exceed provider image limits. Pillow-optional — a graceful no-op when Pillow is absent or the image is already small.

### Changed

- **`read` output ceiling** ([#58](https://github.com/vstorm-co/pydantic-ai-backend/pull/58)) (`src/pydantic_ai_backends/backends/local.py`). A single `read` is bounded at 200k chars: a default read over the cap is truncated to a page with a notice, while an *explicit* `offset`/`limit` that still overflows returns an error so the agent narrows its request instead of flooding context.
- **`glob_info` orders results by modification time (newest first)** ([#58](https://github.com/vstorm-co/pydantic-ai-backend/pull/58)) — with a path tie-break, instead of alphabetically by path — usually what an agent wants.
- **`edit_file` staleness guard** ([#58](https://github.com/vstorm-co/pydantic-ai-backend/pull/58)) (`src/pydantic_ai_backends/toolsets/console.py`). `read_file`/`write_file` record a content fingerprint; `edit_file` refuses with a "read it again" error when a previously-read file changed on disk since it was read. Files never read through the tools are unaffected, and the fingerprint is re-recorded after a successful edit so consecutive edits work. (`hashline_edit` keeps its own per-line hash check.)
- **Python `grep` fallback skips build/cache directories** ([#58](https://github.com/vstorm-co/pydantic-ai-backend/pull/58)) (`node_modules`, `__pycache__`, `dist`, `build`, `.venv`, caches, …) when `ignore_hidden` is on (the default); `ignore_hidden=False` searches everything. ripgrep mode already honors `.gitignore`.

## [0.2.14] - 2026-06-22

### Added

- **Async backend adapter support** ([#55](https://github.com/vstorm-co/pydantic-ai-backend/pull/55), closes [#54](https://github.com/vstorm-co/pydantic-ai-backend/issues/54)) (`src/pydantic_ai_backends/adapter.py`, `src/pydantic_ai_backends/protocol.py`). Lets consumers `await` backend I/O uniformly, whether the underlying backend is sync or natively async:
  - New runtime-checkable `AsyncBackendProtocol` and `AsyncSandboxProtocol` describing the async file/sandbox surface.
  - New `AsyncBackendAdapter` / `AsyncSandboxAdapter` wrapping a sync `BackendProtocol` / `SandboxProtocol`, delegating each call via `asyncio.to_thread`. `AsyncSandboxAdapter.execute()` prefers a native `async_execute()` when present, otherwise offloads `execute()` to a thread.
  - New `ensure_async()` helper that returns native async backends untouched, is idempotent on already-wrapped adapters, and wraps sync backends (selecting the sandbox adapter when the backend exposes `execute`).
  - All five names (`AsyncBackendProtocol`, `AsyncSandboxProtocol`, `AsyncBackendAdapter`, `AsyncSandboxAdapter`, `ensure_async`) are exported from the package root.

### Fixed

- **`AsyncBackendAdapter.read_bytes()` prefers public `read_bytes()` over private `_read_bytes()`** ([#54](https://github.com/vstorm-co/pydantic-ai-backend/issues/54)). Wrapper backends such as pydantic-deep's `BranchOverlay` expose a public `read_bytes()` but may not implement `_read_bytes`, so the adapter now uses the public method when available and only falls back to `_read_bytes` for existing backends — avoiding an `AttributeError` on `read_bytes()`.

### Changed

- **`create_console_toolset` routes all backend I/O through `ensure_async()`** (`src/pydantic_ai_backends/toolsets/console.py`). The console tools (`ls`, `read_file`, `write_file`, `edit_file`, `hashline_edit`, `glob`, `grep`, `execute`) now call `await ensure_async(backend).<op>()` instead of `asyncio.to_thread(backend.<op>, ...)`, so a natively async backend is awaited directly while sync backends keep their thread-offload behavior. The `execute_enabled` gate is still read from the unwrapped backend, and per-path edit locks remain keyed on the raw backend.

## [0.2.13] - 2026-06-17

### Fixed

- **`LocalBackend.write()` / `edit()` no longer double carriage returns on Windows** ([#51](https://github.com/vstorm-co/pydantic-ai-backend/issues/51)) (`src/pydantic_ai_backends/backends/local.py`). `Path.write_text()` opens in text mode, where only `\n` is translated to `os.linesep` on write while existing `\r` is left untouched — so content already containing `\r\n` (commonly emitted by LLMs) became `\r\r\n` on Windows, leaving files with blank lines between every line of code. Content is now normalized before writing so text mode re-adds clean, platform-native line endings.

## [0.2.12] - 2026-06-12

### Added

- **`KubernetesPodSandbox` — run the agent's shell tools inside a Kubernetes pod** ([#46](https://github.com/vstorm-co/pydantic-ai-backend/pull/46)) (`src/pydantic_ai_backends/backends/kubernetes.py`). A new `BaseSandbox` implementation with synchronous methods (matching `DockerSandbox`/`DaytonaSandbox`), usable as a drop-in for any `SessionManager` consumer. `start()` creates the pod and waits for it to become `Ready`; `stop()` deletes it. Two execution modes:
  - **`mode="http"`** (default) talks to an in-pod HTTP exec server on `port` — recommended for long-running tool calls (`npm install`, headless browser, MCP servers).
  - **`mode="api"`** uses the K8s `pods/exec` subresource (needs `pods/exec` RBAC on the caller; fine for short commands). Requires `/bin/sh` and a `timeout` binary in the image.

  Exported as `KubernetesPodSandbox` from the package root (lazy import; requires the optional `kubernetes` extra).

## [0.2.11] - 2026-06-07

### Added

- **`read_file` can return PDFs as `BinaryContent` for document understanding** ([#48](https://github.com/vstorm-co/pydantic-ai-backend/pull/48)) (`src/pydantic_ai_backends/toolsets/console.py`). Previously `create_console_toolset`'s `read_file` returned raster images (png/jpg/jpeg/gif/webp) as `pydantic_ai.BinaryContent` under `image_support`, but PDFs fell through to the text path and were read via the `awk`-based `BaseSandbox.read`, which on a binary PDF emits an empty string — so `read_file("report.pdf")` returned `""` instead of usable content. Documents are now handled as a **separate, independent content kind** from images:
  - New **`document_support: bool = False`** and **`max_document_bytes`** parameters on `create_console_toolset` (default off → fully backward compatible; `image_support` / `max_image_bytes` unchanged).
  - New exported constants, kept disjoint from the image ones: `DOCUMENT_EXTENSIONS` (`{"pdf"}`), `DOCUMENT_MEDIA_TYPES` (`{"pdf": "application/pdf"}`), and `DEFAULT_MAX_DOCUMENT_BYTES` (50 MB). When `document_support=True`, reading a PDF returns `BinaryContent(media_type="application/pdf")` so capable models (OpenAI/Anthropic/Gemini) can read it directly.
  - Internally, `read_file` (both `edit_format` variants) now delegates to two clearly-named helpers — `_maybe_image_content` and `_maybe_document_content` — over a shared `_read_binary_within_limit` (not-found/empty + size-limit guards), removing the prior duplication between the two `read_file` definitions while keeping the image and document seams independent for future per-kind handling (e.g. OCR for images vs. native document understanding / text extraction for PDF/DOCX).

## [0.2.10] - 2026-06-01

### Changed

- **Docstring and import hygiene (internal; no behavior change).** Converted reStructuredText-style double-backtick inline code in docstrings and comments to single-backtick Markdown (108 occurrences), so it renders correctly under the mkdocstrings Markdown handler. Hoisted 12 function-local imports to module top where safe; the optional-dependency `daytona` and `docker.errors` imports were intentionally left local (they must not load when those extras are absent), along with conditional and circular-import-avoidance imports.

### Security

- **Dockerfile generation now validates and escapes untrusted runtime values** - `RuntimeConfig` package names, environment variable names/values, setup commands, and `work_dir` were interpolated directly into `RUN`/`ENV`/`WORKDIR` lines with no validation, so a value like `foo; rm -rf /` could execute arbitrary commands during image build. Package names are now checked against a strict allowlist regex (supporting npm scoped names like `@types/react`), env var names follow the POSIX portable character set, env values and `work_dir` are `shlex`-quoted, and setup commands / env values containing newlines or shell metacharacters are rejected with `ValueError`.
- **Glob negated character class `[!...]` is no longer mistranslated in permission matching** - `_glob_to_regex` copied a glob character class verbatim, so glob negation `[!a]` (meaning "any char except a") became regex `[!a]` (matching the literal `!` or `a`) - the exact opposite, silently inverting deny/allow rules that used negated classes. A leading `!`/`^` after `[` is now emitted as regex `[^...]`.

### Fixed

- **`BaseSandbox.read()` reported wrong line numbers when `offset > 0`** - the `sed | cat -n` pipeline renumbered the slice from 1; it now uses `awk` so line numbers reflect real file positions (matching `StateBackend`/`LocalBackend`).
- **`BaseSandbox.write()` corrupted content via heredoc escaping** - the body was pre-escaping `\`, `$`, and backtick even though the heredoc delimiter is quoted (no shell expansion), doubling backslashes and inserting literal `\$`/`` \` ``. The escaping is removed so content is written verbatim.
- **`BaseSandbox.glob_info()` double-quoted the path and never matched basename globs** - the already `shlex`-quoted path was re-wrapped in single quotes, and `-path '{pattern}'` matched the whole pathname so patterns like `*.py` never matched. The path is now quoted once and the pattern is prefixed (`-path '*/{pattern}'`).
- **npm runtime packages were installed globally and unimportable** - `node-react` and other npm runtimes ran `npm install -g`, so libraries like `react`/`react-dom` were not resolvable from a project's `node_modules`. They are now installed locally into the `work_dir`.
- **`SessionManager.get_or_create()` race could create duplicate sandboxes** - the unguarded check-then-create allowed two concurrent calls for the same `session_id` to each create and start a sandbox, leaking one. A per-session `asyncio.Lock` now serializes creation.
- **`hashline` edits silently ignored a range when `insert_after=True`** - `end_line`/`end_hash` were validated but then ignored by the insert branch. The combination is now rejected with a clear error so callers are not misled.
- **Empty files were reported as "not found"** - the console `read_file` (hashline) and `hashline_edit` tools used `if not raw_bytes`, treating a legitimately empty file (`b""`) as missing. They now use `backend.exists(path)` to distinguish missing from empty.
- **`BaseSandbox.read_bytes()` / `DaytonaSandbox.read_bytes()` returned an error sentinel as file bytes** - on failure they returned `[Error: ...]`-encoded bytes, indistinguishable from a real file beginning with `[Error:`. They now return `b""` on failure (matching the other backends), and `DaytonaSandbox.edit()` uses `exists()` to detect missing files instead of sniffing the sentinel.
- **`CompositeBackend.grep_raw()` swallowed search errors** - when aggregating from root, error strings from the default backend and all string results from routed backends were dropped, so an invalid regex looked like "no matches". The first error encountered is now propagated.
- **`DockerSandbox.execute(timeout=0)` ran unbounded** - `if timeout:` treated `0` like `None`; it now uses `if timeout is not None:`.
- **`DockerSandbox._decode_unknown_text()` had nondeterministic decode order** - when chardet detected an encoding, the candidates were stored in a `set`, so iteration order (detected vs utf-8) was unspecified. It now uses an ordered, deduplicated list with the detected encoding first.
- **`DockerSandbox.write()` ignored the `put_archive` result** - a `False` return (e.g. target is not a directory) was treated as success. It now returns a `WriteResult(error=...)`.
- **`DockerSandbox.__del__` could raise during interpreter shutdown** - the teardown is now wrapped in a broad `contextlib.suppress`, and the explicit `stop()` lifecycle is documented as the reliable path.
- **`StateBackend.grep_raw()` missed an explicitly named hidden file** - with `ignore_hidden=True`, a directly requested hidden path (e.g. `/.env`) fell into the directory branch and matched nothing. An explicitly named file is now looked up in the full file set; the hidden filter applies only to directory walks.
- **Renamed the custom `PermissionError` to `PermissionAskError`** to stop shadowing the builtin `PermissionError` (an `OSError` subclass) for importers of the permissions module. `PermissionError` remains as a deprecated subclass alias for backward compatibility.
- **`create_console_toolset` docstring corrected** - `max_image_bytes` now documents the real 50MB default (was 10MB).
- **`write_file` line count corrected** - the tool reported `content.count("\n") + 1`, which said "1 lines" for empty content and overcounted content ending in a newline. It now uses `len(content.splitlines())`.

### Documentation

- **Documentation accuracy pass.** Rewrote the broken `SessionManager` example in the multi-user guide to use the real async API (`get_or_create`/`release`/`shutdown`, `default_runtime`/`workspace_root`) and corrected its API-reference members (`create_session`/`get_session`/`end_session` did not exist). Added a `DaytonaSandbox` API page and documented the `[daytona]` install extra, replaced the deprecated `PermissionError` with `PermissionAskError` in the permissions reference, fixed invalid Docker runtime keys (`python` → `python-minimal`) and the incorrect `DockerSandbox` `workspace_root` claim, added a hashline edit-format section to the console-toolset guide, and expanded the capability page. Resolved a duplicate `RuntimeConfig` render so `mkdocs build --strict` passes with zero warnings.

## [0.2.9] - 2026-05-24

### Infrastructure

- **CI: bump `astral-sh/setup-uv` to `v8.1.0`** across `ci.yml` (×3) and `publish.yml` — pulled in from Renovate's [Dependency Dashboard #41](https://github.com/vstorm-co/pydantic-ai-backend/issues/41) (rate-limited there). Pinned to the specific patch because `astral-sh/setup-uv` does not maintain a rolling `v8` tag — only `v8.0.0` / `v8.1.0` exist (`v7` and earlier do have rolling majors).
- **CI: bump `actions/setup-python` to `v6`** in `docs.yml` — same source as above; `v6` has a rolling tag so plain `@v6` is used.

No source-code changes — pure CI / dependency-bot housekeeping. Library behaviour unchanged from 0.2.8.

## [0.2.8] - 2026-05-24

### Added

- **`BackendProtocol.exists(path) -> bool` predicate** ([#37](https://github.com/vstorm-co/pydantic-ai-backend/pull/37)) — first-class way to check file presence without sniffing private state (e.g. `StateBackend._files`) or pattern-matching empty-byte returns from `read_bytes()`. Contract: returns `True` only for paths that exist *as regular files*; directories, missing paths, permission errors, and OS-rejected paths (e.g. embedded null bytes) all return `False`. Implementations across every backend:
  - `StateBackend` — dict membership after `_validate_path` / `_normalize_path`.
  - `LocalBackend` — `Path.is_file()` after `_validate_path`; catches `PermissionError`, `ValueError` (POSIX rejects embedded null bytes at the syscall boundary), and residual `OSError` (ELOOP, name too long, ...) to honour the "False for invalid paths" promise.
  - `CompositeBackend` — one-line delegation to `_get_backend(path).exists(path)`.
  - `BaseSandbox` (Docker inherits via default) — `test -f <quoted-path>` over the sandbox shell with a 5 s ceiling.
  - `DaytonaSandbox` — native `self._sandbox.fs.get_file_info(path)`; broad `except Exception` matches the file's existing pattern (mirrors `read_bytes`/`write`); returns `False` on any failure or when `is_dir` is true.

### Changed

- **⚠️ Renamed `_read_bytes` → `read_bytes`** ([#37](https://github.com/vstorm-co/pydantic-ai-backend/pull/37)) — promotes bytes-reading from private (leading underscore) to public on `BackendProtocol`. The semantics are unchanged (empty bytes for missing/erroring reads — `exists()` is now the way to distinguish a real empty file from a missing one), but the rename is **breaking for any caller that was reaching for the private `_read_bytes` name directly** (e.g. earlier versions of the console toolset's `read` / `hashline_edit` tools, which are updated in the same release).
- **Console toolset's `execute` tool now prefers `backend.async_execute(...)` when available** ([#37](https://github.com/vstorm-co/pydantic-ai-backend/pull/37)) — wires up the async-cancellable execution path added in 0.2.7. Backends that don't expose `async_execute` continue to use the existing `asyncio.to_thread(backend.execute, ...)` fallback, so third-party implementations are unaffected.
- **`hashline_edit` is now serialized per `(backend, path)`** ([#37](https://github.com/vstorm-co/pydantic-ai-backend/pull/37)) — concurrent edits to the same file no longer race read-modify-write. Uses a module-level `weakref.WeakKeyDictionary[backend, dict[path, asyncio.Lock]]` so locks are garbage-collected with the backend.

### Infrastructure

- **`renovate.json`** ([#38](https://github.com/vstorm-co/pydantic-ai-backend/pull/38)) — Renovate config landed (first auto-PRs already produced #39/#40).
- **CI: bump `actions/checkout` to `v6`** ([#40](https://github.com/vstorm-co/pydantic-ai-backend/pull/40), Renovate auto-PR).
- **CI: bump `docs.yml` Python to `3.14`** ([#39](https://github.com/vstorm-co/pydantic-ai-backend/pull/39), Renovate auto-PR). The `ci.yml` test matrix stays at `["3.10", "3.13"]`.

## [0.2.7] - 2026-05-14

### Added

- **`LocalBackend.async_execute()` — async, cancellable shell execution** ([#36](https://github.com/vstorm-co/pydantic-ai-backend/pull/36), related to [pydantic-deepagents#93](https://github.com/vstorm-co/pydantic-deepagents/issues/93)) — uses `asyncio.create_subprocess_exec` so that cancelling the calling task immediately kills the subprocess instead of waiting for the thread to finish. The console toolset's `execute` tool now prefers `backend.async_execute(...)` when available and falls back to `asyncio.to_thread(backend.execute, ...)` for backends that don't expose the new method, so third-party backend implementations are unaffected.
  - On Unix, the subprocess is launched with `start_new_session=True` and cancellation/timeout calls `os.killpg(proc.pid, SIGKILL)` so the entire process tree (including grandchildren the shell forked, e.g. `sh -c "sleep 60"`) is reaped. Windows relies on `cmd /c` lifecycle to terminate child processes.
  - Cleanup `await proc.communicate()` after `kill()` is wrapped in `asyncio.shield` so a second cancellation can't leave subprocess pipes dangling.
  - Output is decoded with `errors="replace"` to tolerate non-UTF-8 bytes.

- **Cross-platform shell selection in `LocalBackend`** ([#36](https://github.com/vstorm-co/pydantic-ai-backend/pull/36)) — new static helper `LocalBackend._shell_cmd(command)` returns `["cmd", "/c", command]` on Windows and `["sh", "-c", command]` elsewhere. Both `execute()` and `async_execute()` route through it.

### Fixed

- **`[WinError 2]` crash on Windows when calling `LocalBackend.execute()`** ([#36](https://github.com/vstorm-co/pydantic-ai-backend/pull/36)) — the execute path hardcoded `["sh", "-c", command]`, which is not available on Windows. Now routes through `_shell_cmd()` and uses `cmd /c` on `win32`.

- **Agent task cancellation didn't reach the running subprocess** ([#36](https://github.com/vstorm-co/pydantic-ai-backend/pull/36)) — previously, `execute()` ran on a worker thread via `asyncio.to_thread`, so cancelling the calling task only marked the future as cancelled while the subprocess kept running until completion or timeout. With `async_execute()`, cancellation propagates through to `proc.kill()` (or `killpg` on Unix) immediately.

- **`timeout=0` was silently rewritten to 120 seconds** ([#36](https://github.com/vstorm-co/pydantic-ai-backend/pull/36)) — `execute()` used `timeout or 120`, which treated `0` as falsy and substituted the default. Now uses an explicit `None` check so `0` is honoured (will trigger immediate timeout).

### Changed

- **Extracted `MAX_EXECUTE_OUTPUT = 100_000`** constant in `local.py`, shared by both `execute()` and `async_execute()` truncation paths.

## [0.2.6] - 2026-05-05

### Fixed

- **`CompositeBackend` route matching with trailing slashes** — paths without trailing slashes (e.g. `/foo`) now correctly match routes registered as `/foo/`, matching shell semantics (`ls /tmp` equals `ls /tmp/`). Previously, LLM agents querying paths without trailing slashes would silently fall through to the default backend, breaking file discovery. Added `_normalize_path()` static method and tightened matching to exact-or-child semantics (`== prefix or startswith(prefix + "/")`) to also prevent false positives (e.g. `/foobar` no longer matches `/foo/`). ([#34](https://github.com/vstorm-co/pydantic-ai-backend/pull/34), by [@pawelkiszczak](https://github.com/pawelkiszczak), closes [#33](https://github.com/vstorm-co/pydantic-ai-backend/issues/33))
- **`DockerSandbox.execute` output handling** — fixed crash when `exec_run` returns a generator instead of `bytes` by joining the iterator before decoding.

## [0.2.5] - 2026-04-20

### Fixed

- **Globstar support in `BaseSandbox.glob_info`** — replaced `find -name` with `find -path` so patterns like `**/*.md` match nested files. Previously sandbox backends silently returned empty results for globstar patterns, breaking callers that rely on recursive discovery (e.g. pydantic-deep's skills toolset). Behavior now aligns with `StateBackend`. ([#32](https://github.com/vstorm-co/pydantic-ai-backend/pull/32), by [@ilayu-blip](https://github.com/ilayu-blip))

## [0.2.4] - 2026-04-11

### Added

- **`container_name` parameter on `DockerSandbox`** — stable Docker container name for reuse across restarts. When set, `_ensure_container()` looks for an existing container with that name and reattaches (running containers are reused, stopped containers are restarted). Implies `auto_remove=False` so installed packages, caches, and filesystem state persist between sessions
- **`sandbox_factory` parameter on `SessionManager`** — accepts a `Callable[[str], Any]` to create sandboxes of any type (Docker, Daytona, or custom). When `None`, falls back to the default `DockerSandbox` behavior (fully backward compatible). Exported `SandboxFactory` type alias
- **Lifecycle methods on `BaseSandbox`** — `start()`, `is_alive()`, `stop()`, and `_last_activity` tracking added to the base class so all sandbox types support session management out of the box
- **`start()` method on `DaytonaSandbox`** — no-op (Daytona sandboxes auto-start on creation), added for `SessionManager` compatibility
- **Activity tracking on `DaytonaSandbox`** — `_last_activity` updated on `execute()` calls for idle session cleanup

### Changed

- **`SessionManager` is now backend-agnostic** — no longer hardcoded to `DockerSandbox`. Works with any sandbox that has `start()`, `stop()`, `is_alive()`, and `_last_activity`. Type hints changed from `DockerSandbox` to `Any` for generic usage

## [0.2.3] - 2026-04-06

### Changed

- **Async-safe console toolset** — All synchronous `BackendProtocol` calls in the console toolset are now wrapped in `asyncio.to_thread()`, preventing them from blocking the async event loop. Affects `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`, and `execute` tools. The `BackendProtocol` itself remains synchronous — no changes required for existing backend implementations. ([#26](https://github.com/vstorm-co/pydantic-ai-backend/pull/26), by [@pedroallenrevez](https://github.com/pedroallenrevez))

## [0.2.2] - 2026-03-31

### Changed

- Bump minimum `pydantic-ai-slim` to `>=1.74.0` for compatibility with async `get_instructions` on toolsets

## [0.2.1] - 2026-03-28

### Added

- **`network_mode` parameter on `DockerSandbox`** — Controls container network access. Pass `network_mode="none"` to disable networking entirely, or `"bridge"`, `"host"`, `"container:<name|id>"` for other modes. Defaults to `None` (Docker default). ([#24](https://github.com/vstorm-co/pydantic-ai-backend/pull/24), by [@ggozad](https://github.com/ggozad))

## [0.2.0] - 2026-03-28

### Added

- **`ConsoleCapability`** — new pydantic-ai [capability](https://ai.pydantic.dev/capabilities/) that bundles console tools + instructions + permission enforcement:
  ```python
  from pydantic_ai import Agent
  from pydantic_ai_backends import ConsoleCapability
  from pydantic_ai_backends.permissions import READONLY_RULESET

  agent = Agent("openai:gpt-4.1", capabilities=[ConsoleCapability(permissions=READONLY_RULESET)])
  ```
  - Registers all tools automatically (ls, read_file, write_file, edit_file, glob, grep, execute)
  - Injects console system prompt
  - **Fixes [#23](https://github.com/vstorm-co/pydantic-ai-backend/issues/23)**: `READONLY_RULESET` now actually blocks writes — `prepare_tools` hides denied tools from the model entirely, `before_tool_execute` checks per-path permissions

### Fixed

- **`create_console_toolset` with `READONLY_RULESET` now actually blocks writes** — previously `write=deny` in a ruleset only set `requires_approval=False` (because `"deny" != "ask"`), so tools were registered normally and the agent could write freely. Now tools for denied operations are removed from the toolset entirely. ([#23](https://github.com/vstorm-co/pydantic-ai-backend/issues/23), reported by [@dj-passey](https://github.com/dj-passey))

### Changed

- **Minimum pydantic-ai version bumped to `>=1.71.0`** (capabilities API support)

## [0.1.14] - 2026-03-11

### Fixed

- **DockerSandbox: relative paths and missing file errors** — `read()`, `write()`, and `edit()` now resolve relative paths against the container's `work_dir` instead of `/`. Missing files return clean `"Error: File '...' not found"` messages matching `LocalBackend` behavior. ([#22](https://github.com/vstorm-co/pydantic-ai-backend/pull/22), by [@ret2libc](https://github.com/ret2libc))
- **Fix `test_read_bytes_nonexistent_path` assertion** — Test incorrectly asserted `result is None` instead of `result == b""`, matching the actual `_read_bytes()` return value.

## [0.1.13] - 2026-02-26

### Added

- **Custom tool descriptions** — `create_console_toolset()` now accepts `descriptions: dict[str, str] | None` parameter to override any tool's built-in description

## [0.1.12] - 2026-02-25

### Added

- **`DaytonaSandbox` — cloud sandbox backend** powered by [Daytona](https://daytona.io/) ephemeral sandboxes. Sub-90ms startup, no Docker daemon required. Install with `pip install pydantic-ai-backend[daytona]`.
  - `execute()` via Daytona SDK `sandbox.process.exec()`
  - `_read_bytes()` and `write()` use native Daytona file download/upload APIs (more efficient than shell for binary and large files)
  - `edit()` via read → Python string replace → write (same pattern as `DockerSandbox`)
  - `is_alive()`, `stop()`, automatic cleanup via `__del__`
  - Auth: `DAYTONA_API_KEY` environment variable or `api_key=` constructor parameter
  - Configurable `work_dir` (default: `/home/daytona`) and `startup_timeout`
- New `[daytona]` optional dependency group: `daytona-sdk>=0.9.0`

### Changed

- **Extracted `BaseSandbox` to `backends/base.py`** — `BaseSandbox` is no longer defined inside `backends/docker/sandbox.py`. It now lives in its own module (`pydantic_ai_backends.backends.base`) since it's not Docker-specific. All existing import paths (`from pydantic_ai_backends import BaseSandbox`, `from pydantic_ai_backends.backends.docker import BaseSandbox`) remain fully backward compatible.

## [0.1.11] - 2026-02-24

### Changed

- **Moved tool-specific guidance from system prompt to tool descriptions** — Each console tool (`ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`, `execute`) now carries detailed usage guidance directly in its `description` parameter via exported constants (`LS_DESCRIPTION`, `READ_FILE_DESCRIPTION`, `WRITE_FILE_DESCRIPTION`, `EDIT_FILE_DESCRIPTION`, `GLOB_DESCRIPTION`, `GREP_DESCRIPTION`, `EXECUTE_DESCRIPTION`, plus hashline variants `HASHLINE_READ_FILE_DESCRIPTION`, `HASHLINE_EDIT_DESCRIPTION`). This follows the pattern used by Claude Code and deepagents where guidance lives closest to the tool context.
- **Slimmed `CONSOLE_SYSTEM_PROMPT` and `HASHLINE_CONSOLE_PROMPT`** — Reduced from ~35 lines to 5 lines each. Shell usage rules, git safety, dependency management, debugging tips, and security guidance now live in `EXECUTE_DESCRIPTION`. Edit best practices (surgical edits, re-read after edit) moved to `EDIT_FILE_DESCRIPTION`. File creation rules moved to `WRITE_FILE_DESCRIPTION`.
- **All description constants are exported** from `pydantic_ai_backends` and `pydantic_ai_backends.toolsets` for external customization and override.

## [0.1.10] - 2026-02-20

### Changed

- **Stronger tool preference language in system prompts** — Changed "ALWAYS prefer specialized tools" to "You MUST use specialized tools" in both `CONSOLE_SYSTEM_PROMPT` and `HASHLINE_CONSOLE_PROMPT`. Models now receive a stronger directive to use `read_file`, `glob`, `grep` etc. instead of shell equivalents like `cat`, `find`, `grep`.
- **Stronger execute tool description** — Changed "Do NOT use it for file operations" to "You MUST avoid using file operation commands in the shell" with each tool preference bullet prefixed with "You MUST use". Reduces unwanted `cat`/`grep`/`find` usage in shell.
- **Re-read after edit guideline** — Added "After editing a file, re-read it before making subsequent edits" to both `CONSOLE_SYSTEM_PROMPT` and `HASHLINE_CONSOLE_PROMPT` file operations best practices. Prevents stale-read bugs when auto-formatters or pre-commit hooks modify files on disk after an edit.

## [0.1.9] - 2026-02-20

### Added

- **Hashline edit format** — alternative to `str_replace` that tags each line with a 2-character content hash. Models reference lines by `number:hash` pairs instead of reproducing exact text, eliminating whitespace-matching errors and reducing output tokens. Inspired by [Can Bölük's hashline research](https://can.ac/b/hashline) which showed +5 to +64pp accuracy improvement across 16 models.
  - `edit_format` parameter on `create_console_toolset()` — set to `"hashline"` to opt in (default: `"str_replace"`)
  - `edit_format` parameter on `get_console_system_prompt()` — returns matching system prompt
  - When `edit_format="hashline"`:
    - `read_file` returns lines as `1:a3|content` (number:hash|content)
    - `hashline_edit` tool replaces `edit_file` — reference lines by number+hash, no old-text reproduction needed
    - Operations: replace single line, replace range, insert after, delete
    - Hash validation: edit rejected if file changed since last read
  - New `pydantic_ai_backends.hashline` module with pure utility functions:
    - `line_hash()` — generate 2-char hex content hash for a line
    - `format_hashline_output()` — format file content with hashline tags
    - `apply_hashline_edit()` — apply a hashline edit with hash validation
    - `apply_hashline_edit_with_summary()` — same but returns human-readable summary
  - `HASHLINE_CONSOLE_PROMPT` — system prompt for hashline mode
  - `EditFormat` type alias exported from package

## [0.1.8] - 2026-02-19

### Fixed

- **`DockerSandbox.grep_raw()` searched entire filesystem by default**: When no `path` argument was provided, `grep_raw()` defaulted to `"/"` instead of `"."`, causing grep to scan the entire container filesystem. This made pathless grep calls extremely slow (minutes) and returned irrelevant matches from system files. Now defaults to the current working directory. ([#13](https://github.com/vstorm-co/pydantic-ai-backend/pull/13))

## [0.1.7] - 2025-02-16

### Added

- **Image support in `read_file`**: When `image_support=True` is passed to `create_console_toolset()`, reading image files (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`) returns a `BinaryContent` object that multimodal models can see, instead of garbled text.
  - `image_support` parameter on `create_console_toolset()` (default: `False`)
  - `max_image_bytes` parameter to limit image file size (default: 50MB)
  - `IMAGE_EXTENSIONS`, `IMAGE_MEDIA_TYPES`, `DEFAULT_MAX_IMAGE_BYTES` constants exported from the package
- **Documentation**: Expanded guides for backends, console toolset, permissions, and multi-user setups.

## [0.1.6] - 2025-02-07

### Added

- **`max_retries` parameter for `create_console_toolset()`**: Allows configuring the maximum number of retries for all console tools (`write_file`, `edit_file`, `read_file`, `ls`, `glob`, `grep`, `execute`). When the model sends invalid arguments (e.g. missing a required field like `content` for `write_file`), the validation error is fed back and the model can self-correct up to `max_retries` times. Defaults to 1 (unchanged) for backward compatibility. ([pydantic-deepagents#25](https://github.com/vstorm-co/pydantic-deepagents/issues/25))

## [0.1.5] - 2025-01-28

### Changed

- `DockerSandbox.read()` now supports **any file extension** instead of a hardcoded whitelist.
  Uses a three-tier approach: known extensions → mimetypes detection → binary detection fallback.
  Binary files return `[Binary file - cannot display as text]` instead of raising an error.
  ([#9](https://github.com/vstorm-co/pydantic-ai-backend/pull/9))

### Fixed

- `DockerSandbox.stop()` and `__del__` now handle edge cases where `_container` attribute
  may not exist, preventing `AttributeError` during cleanup.

## [0.1.4] - 2025-01-22

### Changed

- **README**: Complete rewrite with centered header, badges, Use Cases table, and vstorm-co branding
- **Documentation**: Updated styling to match pydantic-deep pink theme

### Added

- **Custom Styling**: docs/overrides/main.html, docs/stylesheets/extra.css
- **Abbreviations**: docs/includes/abbreviations.md for markdown expansions
- **FAQ Section**: Expanded getting-help.md with common questions

## [0.1.3] - 2026-01-22

### Fixed

- `DockerSandbox.edit()` now handles multiline strings correctly. Replaced sed/grep-based
  implementation with Python string operations, which naturally handle newlines and special
  characters without shell escaping issues. ([#6](https://github.com/vstorm-co/pydantic-ai-backend/pull/6))

### Changed

- Added `edit()` as an abstract method in `BaseSandbox` to make the interface explicit
- Docker tests now use shared fixtures (`scope="module"`) for faster test execution

## [0.1.2] - 2026-01-21

### Added

- **Fine-grained Permission System** - Pattern-based access control for file operations and shell execution
- **Pre-configured Permission Presets** (DEFAULT, PERMISSIVE, READONLY, STRICT)
- **Permission Integration** with `LocalBackend` and `create_console_toolset()`

### Fixed

- `DockerSandbox.execute()` no longer incorrectly escapes commands when timeout is specified.

## [0.1.1] - 2026-01-20

### Added

- `ignore_hidden` parameter to `grep_raw()` in `BackendProtocol`

## [0.1.0] - 2025-01-17

### Added

- Initial release — `LocalBackend`, `StateBackend`, `CompositeBackend`, `DockerSandbox`, `SessionManager`, Console Toolset

## [0.0.4] - 2025-01-16

### Added

- `volumes` parameter to `DockerSandbox`
- `workspace_root` parameter to `SessionManager`

## [0.0.1] - 2025-12-28

### Added

- Initial release extracted from pydantic-deep
