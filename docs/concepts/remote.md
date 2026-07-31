# Remote Sandboxes

`RemoteSandbox` runs your agent's sandbox in **another process**, so your
application never needs Docker access. A separate service — `sandboxd` — owns the
Docker socket and rents out sandboxes over HTTP.

```python
from pydantic_ai_backends.remote import RemoteSandbox

sandbox = RemoteSandbox("http://sandboxd:8080", token="...")
print(sandbox.execute("python -c 'print(1+1)'").output)  # "2"
sandbox.stop()
```

The session — and the container behind it — opens on the **first operation**, so
an agent granted a sandbox it never touches costs nothing.

## Why this exists

If your application runs in a container and wants a Docker sandbox, there are
only three options:

| Approach | What it costs you |
|---|---|
| Mount `/var/run/docker.sock` into the app | The socket is an unauthenticated API for **root on the host**. Anything that can reach it can start a privileged container that bind-mounts `/`. |
| Docker-in-Docker | Needs `--privileged`. Same outcome, more moving parts. |
| **`sandboxd` + `RemoteSandbox`** | The app holds only an HTTP token. One small service holds the socket. |

The third is what this module is. No docker-in-docker.

## Installation

The client needs only `httpx`; the service needs FastAPI and the Docker SDK, so
they are separate extras — install the client extra in your app image and the
server extra in the sandbox service image.

```bash
pip install pydantic-ai-backend[remote]   # RemoteSandbox (client)
pip install pydantic-ai-backend[server]   # sandboxd (service)
```

## Running the service

```python
# sandboxd_app.py
from pydantic_ai_backends.remote.server import SandboxdConfig, create_app

app = create_app(
    SandboxdConfig(
        token="a-long-random-secret",
        runtimes={
            "python": "python:3.12-slim",
            "node": "node:20-slim",
        },
        default_runtime="python",
        max_sessions=20,
        mem_limit="1g",
        cpus=2.0,
        network_mode="none",
        idle_timeout=1800,
        # Files and installed packages survive a reaping.
        workspace_root="/workspaces",
        persist_containers=True,
        workspace_ttl=14 * 24 * 3600,
        # One tenant cannot occupy the whole pool.
        max_sessions_per_tenant=5,
    )
)
```

```bash
uvicorn sandboxd_app:app --host 0.0.0.0 --port 8080
```

In Compose, the socket is mounted **only** into this service, and the service is
never published to the edge:

```yaml
services:
  app:
    # no docker socket here
    environment:
      SANDBOXD_URL: http://sandboxd:8080
      SANDBOXD_TOKEN: ${SANDBOXD_TOKEN}
    networks: [backend]

  sandboxd:
    build: ./sandboxd
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - sandbox_workspaces:/workspaces
    environment:
      SANDBOXD_TOKEN: ${SANDBOXD_TOKEN}
    networks: [backend]        # deliberately no `ports:`

volumes:
  sandbox_workspaces:
```

## The runtime allowlist

A request names an **alias** and nothing else. What that alias runs — the image,
the ceilings, whether the sandbox gets a network — is the operator's decision,
per runtime:

```python
from pydantic_ai_backends.remote.server import SandboxRuntime, SandboxdConfig

SandboxdConfig(
    token=token,
    runtimes={
        # A ready-made image: starts as fast as a pull.
        "python": SandboxRuntime(image="python:3.12-slim", mem_limit="1g", cpus=1.0),

        # A built runtime: the first session installs the packages into an image,
        # later ones hit the cache. Worth it when per-session installs would
        # dominate.
        "datascience": SandboxRuntime(runtime="python-datascience", mem_limit="4g", cpus=2.0),

        # The one runtime allowed to reach the network, deliberately.
        "scraping": SandboxRuntime(runtime="python-scraping", network_mode="bridge"),
    },
    default_runtime="python",
    # Defaults for any runtime that names none of its own.
    mem_limit="1g",
    cpus=1.0,
)
```

Ceilings are per runtime because they are not one number for a whole service: a
notebook-style data runtime needs several gigabytes where a plain shell needs a
few hundred megabytes, and forcing one value on both either starves the first or
over-commits the host for the second.

A ceiling a runtime leaves unset takes the service-wide value, which is a
**default rather than a maximum** — a runtime may name more. What bounds the host
is `max_sessions` times the largest runtime ceiling, and that is the number to
size for.

`runtime=` accepts the name of any
[built-in runtime](docker.md#built-in-runtimes) or a `RuntimeConfig` of your own.
Its `work_dir` is overridden with the service's, so the workspace volume, the
archive endpoints and a client's paths cannot end up disagreeing about where
files live.

### Shipped catalogues

| Catalogue | What it is |
|---|---|
| `DEFAULT_RUNTIMES` | What you get when you name nothing: `python` and `node`, both ready-made, no ceilings of their own. A default that built package sets would make a fresh deployment's first session take minutes. |
| `SUGGESTED_RUNTIMES` | A fuller catalogue to adopt or copy from — data, analytics, documents, scraping, TypeScript, Go, Rust, each with a memory ceiling that suits it. |

```python
from pydantic_ai_backends.remote.server import SUGGESTED_RUNTIMES, SandboxdConfig

SandboxdConfig(token=token, runtimes=SUGGESTED_RUNTIMES, default_runtime="python")
```

Not the default, because every entry is a commitment on the operator's own host:
`python-datascience` builds an image on first use and is given four gigabytes, and
`python-scraping` is the one entry allowed the network. Those are decisions worth
reading before enabling.

A bare string still works wherever a runtime entry is expected —
`runtimes={"python": "python:3.12-slim"}` is `SandboxRuntime(image=...)`.

## Security model

A process that can talk to the Docker daemon is root-equivalent on the host, so
`sandboxd` is the most sensitive process in a deployment. It is built around
that:

- **Clients choose nothing about the container.** Image, mounts, network mode and
  every resource ceiling come from `SandboxdConfig`. A request carries at most a
  runtime **alias**, validated against the allowlist — so a client cannot run an
  image of its choosing.
- **Session ids are pattern-checked** before they reach a path or a container, so
  one cannot traverse a directory.
- **Each session gets its own token.** The service token is the only one that may
  open or enumerate sessions; a session token reaches only its own sandbox.
- **The token is verified before existence is revealed**, so an unauthenticated
  caller cannot enumerate session ids by watching `401` turn into `404`.
- **`network_mode` defaults to `"none"`.** A service handing sandboxes to
  untrusted code should not give them the network unless that is deliberate.
- Bind it to a private network. It has no TLS and no rate limiting of its own.

## Sessions that outlive a run

By default, opening a session id that is already in use is a `409` — silently
sharing another caller's sandbox is the worse failure. Pass `reuse=True` when a
sandbox is meant to span several runs, such as one conversation:

```python
# First turn: creates the session.
sandbox = RemoteSandbox(url, token=token, session_id=session_id, reuse=True)
sandbox.start()
sandbox.write("/notes.md", "draft")

# Later turn, new process even: attaches to the same sandbox.
sandbox = RemoteSandbox(url, token=token, session_id=session_id, reuse=True)
sandbox.start()
assert sandbox.read_bytes("/notes.md") == b"draft"
```

A `runtime` that disagrees with the open session is **refused**, not honoured.
Honouring it would mean replacing a live sandbox and discarding the files the
caller came back for.

### Choosing what the session id keys on

The session id is the only thing you choose, so it is what decides who shares
files with whom:

| Keyed on | Who shares the sandbox | Good for |
|---|---|---|
| a run id | nobody — ephemeral | one-shot analysis |
| a conversation id | **everyone in that chat**, group chats included | a Slack channel, a web thread |
| a user id | that person across all their chats | a personal workspace |
| an agent id | **everyone who talks to that agent** | one shared machine for a team |

The last two are data-sharing decisions rather than technical ones: with an
agent-keyed sandbox, any user reads what any other user wrote. Make that visible
wherever the choice is made.

Keep ids unguessable — derive them from a UUID you generated, never from a
sequential id or user input — and inside 64 characters.

## Making it fast on a small host

Four settings, and on a 4 GB box each one is worth more than it sounds.

```python
SandboxdConfig(
    token=token,
    runtimes=SUGGESTED_RUNTIMES,
    prewarm=True,            # build and pull the allowlist at startup, not on a request
    persist_containers=True, # a reaped session restarts instead of rebuilding
    tmpfs_size="64m",        # scratch writes stay in RAM, off the overlay
    cpu_shares=1024,         # weight rather than a hard cap, so idle cores get used
    cpus=None,
)
```

**`prewarm`** pulls and builds the whole allowlist in the background as the
service starts, sequentially so several builds do not fight over one small host.
Without it the first session on a built runtime pays for the image — measured at
about eleven seconds for a pandas runtime — in the middle of somebody's request.
A runtime that will not build is logged and skipped; one bad entry does not stop
the others being ready.

**`tmpfs_size`** gives every sandbox an in-memory `/tmp`. Otherwise scratch files
land in the container's write layer, which is slower and grows the disk. The
mount is given `exec` explicitly, because Docker's default is `noexec` and that
breaks installing any package that builds from source.

Its pages are charged to the container's own memory cgroup, so it comes *out of*
`mem_limit` rather than being extra: a sandbox limited to 512m with a 64m `/tmp`
has 448m left for its processes, and one that tries to exceed the limit through
`/tmp` is killed by its own cgroup rather than troubling the host. Keep it small
when many sessions are open at once.

**`cpu_shares` versus `cpus`.** A hard `cpus=1.0` means a sandbox never exceeds
one core — including when the other three are idle, which on a small VPS is
usually the wrong trade. A weight only applies under contention: one active
sandbox may use the whole machine, and several are still divided fairly. Set
both if you want a ceiling *and* fair sharing.

**Builds use BuildKit when the `docker` CLI is present**, which is what lets a
package cache survive between builds — editing one package in a runtime then
re-downloads nothing. The Python SDK has no BuildKit support at all and its
builder rejects cache mounts outright, so without the CLI the generated
Dockerfile falls back to discarding the cache instead. `GET /policy` reports
which builder is in force, so it is never a mystery.

Images superseded by a build are removed as it finishes. The tag embeds a digest
of the runtime, so every edit mints a new one and would otherwise leave a few
hundred megabytes behind for good — an image still backing a running container is
left alone.

### The library matters more than the tuning

Measured on one 188 MB CSV, the same `GROUP BY` in each:

| Library | Import | Time | Peak RSS | Under a 384 MB ceiling |
|---|---|---|---|---|
| pandas | 97 MB | 1.7 s | 570 MB | **killed, exit 137** |
| Polars | 43 MB | 0.2 s | 502 MB | survives, 0.7 s |
| DuckDB | 51 MB | 0.2 s | **312 MB** | survives, 0.6 s |

pandas materialises the whole frame and copies freely, so it needs roughly three
times the file in memory. DuckDB and Polars stream, so the same answer fits in a
sandbox that pandas cannot run in at all — and they are about eight times faster
into the bargain.

That single swap does more for how many sessions fit than every container setting
on this page combined: sizing an analysis sandbox for DuckDB rather than pandas
roughly doubles how many run at once on a fixed amount of RAM. It is also the
difference between a readable error and a silent one — DuckDB raises an
`OutOfMemoryException` the agent can read and retry around, where pandas gets
`SIGKILL` and the tool call just reports exit 137.

Hence `python-analytics` in [the catalogue](docker.md#built-in-runtimes) carries a
1 GB ceiling while `python-datascience` carries 4 GB: the second number is pandas'
appetite, not the task's. Keep pandas available — most model-written code reaches
for it first — but point agents that work on real files at the columnar runtime.

## What survives, and for how long

Three settings, and they cover different things. Getting one wrong looks like the
agent forgetting its work:

| Setting | Preserves | Without it |
|---|---|---|
| `workspace_root` | the **work directory**, on a host volume per session | files vanish when the container is reaped |
| `persist_containers` | the container's **write layer** — packages installed with `pip` or `apt` | the agent reinstalls its dependencies after every idle timeout |
| `workspace_ttl` | nothing — it **reclaims** unused workspaces | workspaces accumulate on disk for ever |

Retention pulls the three apart, and the defaults say what an agent's user
expects: **files are kept for ever, builds are reclaimable.**

```python
SandboxdConfig(
    workspace_root="/var/lib/sandboxd",
    persist_containers=True,
    workspace_ttl=None,          # the default: the agent's files are the work
    container_ttl=30 * 86400,    # its installs are rebuildable, so reclaim them
)
```

`container_ttl` removes a *stopped* sandbox container that has been stopped for
that long, keeping its workspace untouched — the disk equivalent of deleting a
build directory and keeping the source. What a session installed inside itself can
be installed again; what it wrote to `/workspace` cannot. `workspace_ttl` is the
opposite knob and deletes the files, so it stays `None` unless a retention policy
says otherwise.

`workspace_root` alone is the trap: `/workspace` comes back but
`pip install pandas` does not, because it landed outside the volume. If a session
is meant to feel like "the same machine", set both.

`persist_containers` names each container after its session, so a reaped session
is *stopped* rather than discarded and the next attach restarts it. The cost is
that stopped containers stay on the host until the session is purged — which is
why it is off by default.

### Ending a session for good

```python
sandbox.stop()              # ends the turn, keeps the files for the next one
sandbox.stop(purge=True)    # discards the container and the workspace
```

Purge when the thing the session belonged to is gone — a deleted conversation, a
user who left. `workspace_ttl` is the safety net for everything nobody
remembered to purge: a workspace with no open session, untouched for longer than
the TTL, is swept. The clock runs from when the session was last *opened*, so an
active session is never swept however old its directory is.

## A working set, not a hard cap

`max_sessions` counts **live sandboxes**, and by default a full pool refuses the
next request with `429`. That is the wrong answer when the pool is full of
sessions nobody is using: an agent is turned away while a hundred containers sit
idle holding slots.

`evict_idle_after` changes that. At the ceiling, the least recently used session
idle for at least that long is closed to make room:

```python
SandboxdConfig(
    token=token,
    max_sessions=100,          # live sandboxes — the working set
    evict_idle_after=120,      # idle two minutes? your slot can be reused
    workspace_root="/var/lib/sandboxd",
    persist_containers=True,
)
```

With a workspace on disk, eviction costs the evicted session nothing but its
container: its next request re-attaches, finds its files, and pays a container
start — measured at 0.09 s for a persisted one. So the number of sessions that
may *exist* becomes unbounded while the number *running at once* stays at 100.

Two deliberate limits on it:

- **A busy session is never evicted.** Only sessions idle for at least
  `evict_idle_after` are candidates, because killing an agent's work to serve
  somebody else's first request is worse than making them wait. With every
  session genuinely busy the caller still gets `429` — backpressure is the honest
  answer there, not an unbounded queue.
- **It requires `workspace_root`.** Evicting a session whose files live only in
  its container would discard them silently, so the configuration refuses rather
  than making that trade quietly.

`max_sessions=None` removes the ceiling entirely, for hosts where something else
does the bounding.

## Capacity per tenant

`max_sessions` caps the service. That is not enough when one application serves
many tenants: the busiest one fills the pool and everybody else gets `429`.

```python
sandbox = RemoteSandbox(url, token=token, session_id=session_id, tenant=str(org_id))
```

With `max_sessions_per_tenant` set, a tenant at its own ceiling is refused while
others are still served. The label is **capacity only** — it grants nothing and
authorizes nothing, and only a holder of the service token can open a session at
all. It is reported back in `GET /sessions` and `GET /policy` so an operator can
see who is holding what.

## Nothing starts until it is used

Constructing a `RemoteSandbox` performs no I/O. The session is opened by the
first operation, which means a container starts only when the model actually
reaches for a file or a command:

```python
sandbox = RemoteSandbox(url, token=token, session_id=session_id)   # no HTTP yet
agent = Agent("openai:gpt-4.1", capabilities=[ConsoleCapability(backend=sandbox)])

result = agent.run_sync("What is 2 + 2?")   # answered without any container
result = agent.run_sync("Write hello.py")   # this is what opens one
```

That matters most for an agent that *may* need a sandbox: granting the capability
no longer costs a container per run. Call `start()` yourself only to pre-warm one
before a latency-sensitive turn.

Opening is guarded, so two operations arriving at once on a thread pool open one
session rather than racing each other into a `409`. A failure to open degrades
like any other operation failure — the tool call reports an error, the run
continues.

## Browsing files without a sandbox

A workspace outlives the sandbox that wrote it, so listing and reading it should
not cost a container start. Opening a conversation from last week just to see what
the agent produced would otherwise boot a container, wait for it, and reap it
minutes later.

`/workspaces/{session_id}/ls` and `/workspaces/{session_id}/read` serve the host
volume directly. They work for a session reaped long ago, and there is a typed
client for them:

```python
from pydantic_ai_backends.remote import WorkspaceArchive

archive = WorkspaceArchive("http://sandboxd:8080", token=service_token)
for entry in archive.ls(session_id):
    print(entry["path"], entry["size"])
print(archive.read(session_id, "report.md"))
```

Four things to know:

- **Requires `workspace_root`.** Without it there is nothing stored to read, and
  the endpoints answer `409` naming the setting rather than pretending the
  workspace is empty.
- **Service token only.** A reaped session has no token of its own left, and the
  intended caller is an application proxying file views to its users *after*
  applying its own authorization.
- **It raises rather than degrading**, unlike `RemoteSandbox`. No model is waiting
  on it, and an application answering "show me my files" needs to tell "there are
  none" apart from "the service is misconfigured".
  `WorkspaceArchiveError.status_code` carries what the service said.
- **Only the work directory is stored.** A file the agent wrote to `/tmp` is not
  in the volume and so not in the archive.

Paths are interpreted relative to the sandbox's work directory, and an absolute
in-container path works too — so a UI can hand back exactly the path a live
listing showed it.

### Why this is the most careful code in the module

These endpoints read the host filesystem, so path containment is the only thing
between a caller and the rest of the machine. Two ways in are closed, both by
resolving first and checking after:

- `..` in a requested path.
- **A symlink the sandbox itself planted.** Untrusted code in a container can run
  `ln -s /etc/shadow notes.txt`. Inside the container that points at the
  container's own file; read from the host side it would resolve to the host's. So
  a path that resolves outside the workspace is refused even when the link sits
  inside it, and such an entry is omitted from a listing rather than reported.

### Never hand a session token to a browser

Worth stating separately, because the fix is not obvious: the **session** token
authorizes `/exec` as well as `/ls` and `/read`. Putting one in a web page gives
whoever opens DevTools a shell inside the sandbox. The service token is worse.

Proxy instead — your backend holds the token, checks that this user may see this
conversation, and forwards only listing and reading.

## Failure behaviour

File and command operations **degrade rather than raise**: a transport failure
surfaces the same way a missing file does — `b""`, `[]`, or an `Error: ...`
string — matching `LocalBackend` and `DockerSandbox`. A tool call must not take
down an agent run because a socket blipped.

`start()` is the exception and does raise: a caller who cannot get a sandbox at
all needs to know why.

## Capacity and reaping

- `max_sessions` caps concurrency; beyond it, opening a session answers `429`
  rather than starting unbounded containers. `max_sessions_per_tenant` applies the
  same ceiling per `tenant` label.
- `idle_timeout` reaps sessions that have gone quiet, and the reaping is visible
  to the service's own bookkeeping — a reaped session's token and event log are
  released with it.
- `execute_timeout` clamps every command, so one client cannot occupy a worker
  indefinitely.
- `workspace_ttl` sweeps workspaces no session has opened for that long, on the
  same interval as idle reaping.
- Resource sampling is cached for a few seconds and taken concurrently off the
  event loop. One Docker stats call costs over a second — it waits for a second
  sample to compute a CPU rate — so a listing that sampled each sandbox in turn
  would take a second per session and hold up everything else meanwhile.
- Blocking sandbox calls run on the service's own thread pool (`max_workers`),
  kept separate from asyncio's shared default one because sandbox commands are
  long.

## Observability

| Endpoint | Auth | What it gives you |
|---|---|---|
| `GET /` | none | What the service is, and its mounted endpoints |
| `GET /healthz` | none | Liveness, session count, capacity |
| `GET /policy` | service token | The ceilings and image allowlist actually in force |
| `GET /sessions` | service token | Every open session; `?usage=true` also samples memory and CPU |
| `GET /sessions/{id}` | session or service token | One session, without reviving a dead sandbox |
| `GET /sessions/{id}/events?after=<seq>` | session or service token | The session's operation log, for incremental polling |

The activity log records what each operation addressed, whether it succeeded and
how long it took — never file contents or command output, which would turn an
audit trail into a data leak that also grows without bound. It is a bounded ring
buffer per session.

### Dashboard

`SandboxdConfig(ui_enabled=True)` serves a dashboard at `/ui`: one
self-contained HTML file, no build step and no CDN, so it works offline and
behind a strict CSP. Three views:

**Sessions** — capacity at a glance, and every open session with its tenant, idle
time and memory against its own ceiling. Clicking a row opens its workspace; a
session can be created here with a runtime, an id and a tenant.

![sandboxd dashboard, sessions view](../assets/dashboard-sessions.png)

**Workspace** — one session at full width: a **Terminal** with command history, a
**Files** browser, the **Activity** log and **Info**.

![sandboxd dashboard, workspace view with the terminal](../assets/dashboard-workspace.png)

**Runtimes & policy** — the allowlist as cards (image, description, memory, CPU,
processes, network, and whether the first session builds an image), the
`SandboxdConfig` that produces it, and every service default and retention
setting in force.

![sandboxd dashboard, runtimes and policy view](../assets/dashboard-runtimes.png)

The Files browser reads the **live sandbox** by default and can switch to the
**stored workspace**, which is served from the host volume. That distinction is
worth knowing: reading a stopped session live restarts its container, while the
stored workspace needs no container at all — and shows only the work directory.

Off by default: the page asks a human for the **service** token, and that token
can start containers on the host. Keep it on localhost or a private network.

## Using it with an agent

`RemoteSandbox` implements the same synchronous surface as `DockerSandbox`, so it
drops into a console toolset or `SessionManager` unchanged:

```python
from pydantic_ai import Agent
from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.remote import RemoteSandbox

sandbox = RemoteSandbox("http://sandboxd:8080", token=token, session_id=session_id)
sandbox.start()

agent = Agent(
    "openai:gpt-4.1",
    capabilities=[ConsoleCapability(backend=sandbox)],
)
```

Passing the sandbox to the capability keeps it out of your deps type — see
[Capability](capability.md#a-capability-owned-backend).
