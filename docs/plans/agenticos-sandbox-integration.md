# Pluggable sandbox backends for AgenticOS

Implementation description for letting an AgenticOS agent be granted files and a
shell, backed by one of three interchangeable backends: `state`, `daytona` or
`docker`.

Spans two repositories:

- **pydantic-ai-backend** — supplies the backends, the console toolset and the
  `sandboxd` service. Library changes are listed under
  [Library changes](#library-changes); the ones marked *done* have landed.
- **AgenticOS** — consumes them as one capability. Listed under
  [AgenticOS changes](#agenticos-changes).

---

## The problem

AgenticOS already runs model-written Python through `code_execution`, which uses
the Monty sandbox: no network, no filesystem, a restricted stdlib subset. That
restriction is what makes it safe to grant broadly, and it is also what makes it
useless for the next thing agents are asked to do — read a repository, write a
file, run a test suite.

That needs a real filesystem and a real shell, which needs a real sandbox, which
has to run *somewhere*. The two obvious placements are both wrong:

- **Mounting `/var/run/docker.sock` into the `app` container.** The Docker socket
  is an unauthenticated API for root on the host. An application holding it can
  start a privileged container that bind-mounts `/`. In a multi-tenant
  deployment this is not a hardening question, it is a full compromise.
- **Docker-in-Docker.** Requires `--privileged`, which is the same outcome with
  more moving parts.

So the sandbox belongs in a separate process that owns Docker and exposes
nothing else — and the application talks to it over HTTP.

## Three backends, two configuration planes

The three backends are not three variations of one case. They differ in *who is
allowed to configure them*:

| Backend | Runs where | Configured by |
|---|---|---|
| `state` | in the app process | agent author (nothing to configure) |
| `daytona` | Daytona's cloud | agent author, plus an API key per organization |
| `docker` | `sandboxd`, next to the Docker socket | **the operator, not the agent author** |

The consequence is the load-bearing design decision here:

> **The agent spec chooses a backend. It never chooses an image, a mount, a
> network mode or a resource ceiling.**

If a spec could name a Docker image, an author could name one whose entrypoint
mounts the host — and specs are authored in a browser by whoever holds `edit` on
an agent. `sandboxd` already enforces this: a request carries at most a runtime
*alias*, validated against a server-side allowlist, and everything else comes
from `SandboxdConfig`.

## Session scope

Configurable per agent, because the shapes of work genuinely differ — and it is
not a two-value switch. It is a choice of **what the session id keys on**, which
is the same as choosing who shares files with whom.

```python
class SandboxConfig(BaseModel):
    backend: Literal["state", "daytona", "docker"] = "state"
    session_scope: Literal["run", "conversation", "user", "agent"] = "run"
    runtime: str | None = None  # alias from the sandboxd allowlist; the
    # ceilings behind it are the operator's
    include_execute: bool = True
```

| scope | key | who shares the sandbox | typical use |
|---|---|---|---|
| `run` | `run_id` | nobody — ephemeral | one-shot analysis |
| `conversation` | `conversation_id` | **everyone in that chat**, group chats included | a Slack channel, a Mattermost channel, a web thread |
| `user` | `user_id` | that person across all their chats | a personal workspace |
| `agent` | `agent_id` | **everyone who talks to that agent** | one shared machine for a team |

`"run"` is the default: it releases the sandbox after every turn, so it cannot
sit on a slot in the session pool. Every key is prefixed with the organization.

A Slack or Mattermost channel *is* a conversation, so `conversation` scope gives
group chats exactly the shared environment people expect from one.

### Scope is a data-sharing policy

This is the part that must be visible in the Builder as a policy rather than as
one more technical option:

- **`agent`** — any user of the agent reads files another user wrote. That is a
  deliberate crossing of a boundary between people in one organization, so it
  should carry its own permission scope (`sandbox:shared`) rather than being a
  value of an enum alongside `run`.
- **`user` in a group chat is incoherent.** Three people in a channel get three
  sandboxes; the agent writes a file for one of them and tells the other two
  about a file they cannot see. Not a security hole — a product bug. Either
  refuse the combination at publish or document `user` as 1:1 only.
- **`conversation`** — sharing is scoped exactly like the conversation itself,
  which is already what `ConversationShare` enforces. The safest scope that still
  supports real work on files.

### `state` cannot honour anything but `run`

`StateBackend` lives in the app process, so any scope beyond `run` would be a lie
the moment there are two workers, or one restarts. Refuse the combination at
publish rather than ignoring the field: silently meaning something else is worse
than saying no.

### Identity across surfaces

`AgentDeps.user_id` is `str | None`. On web and WebSocket it is a platform user;
on Slack and Mattermost it is that system's id; on an API-key run it may be
absent. So `user` scope needs a normalised identity (`{surface}:{external_id}`,
or a mapped platform user) and must refuse at publish when there is none —
falling back to a shared sandbox would silently merge people's files.

**Recommendation: ship `run`, `conversation` and `agent` first.** Add `user` once
channel identity is normalised.

### Deriving the session id

`sandboxd` constrains ids to `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, so 1–64
characters. Two hyphen-stripped UUIDs are 65 with a separator — one over.

```python
SCOPE_KEYS = {
    "run": lambda ctx: ctx.run_id,
    "conversation": lambda ctx: ctx.conversation_id,
    "user": lambda ctx: ctx.user_id,
    "agent": lambda ctx: ctx.agent_id,
}

session_id = f"{organization_id.hex[:8]}-{SCOPE_KEYS[scope](ctx).hex}"  # 41 chars
```

**The organization prefix is for readability in the dashboard, not for
isolation.** Isolation comes from the keys being `uuid4`
(`default=uuid.uuid4` on both tables) — globally unique and unguessable. Worth
stating explicitly, because a future reader who believes the prefix is the
security boundary will either shorten it carelessly or refuse to touch it for the
wrong reason.

The organization also goes in `tenant=`, which is a separate thing: a capacity
label the service counts against `max_sessions_per_tenant`. It grants nothing.

### Capacity per scope

| scope | sandboxes | `max_sessions=20` is… |
|---|---|---|
| `agent` | one per agent | plenty |
| `conversation` | one per warm chat | nothing, for a Slack workspace with 200 channels |
| `user` | one per active user | sized by headcount |
| `run` | one per turn, released immediately | plenty, but constant container churn |

So `max_sessions_per_tenant` is not optional: without it one talkative
organization occupies the pool of the whole installation.

### What survives a reaping

Three service settings, covering different things, and getting one wrong looks
like the agent forgetting its own work:

| Setting | Preserves |
|---|---|
| `workspace_root` | the work directory, on a host volume per session |
| `persist_containers` | the container's write layer — `pip`/`apt` installs |
| `workspace_ttl` | nothing; it reclaims workspaces nobody opens any more |

`workspace_root` alone is the trap: `/workspace` comes back after an idle
timeout, but installed packages do not, because they landed outside the volume.
"One shared machine for the team" needs both, or the agent reinstalls its
dependencies every thirty minutes.

### Retention

"Return to a conversation from last week and see the files" makes
`workspace_root` **mandatory** for every scope but `run`, and ties the lifetime
of a workspace to the lifetime of whatever keys it:

| scope | workspace dropped when |
|---|---|
| `run` | immediately after the run |
| `conversation` | the conversation is deleted, or the TTL expires |
| `user` | the user leaves the organization |
| `agent` | the agent is deleted |

Only AgenticOS knows a conversation was deleted, so it calls
`RemoteSandbox.stop(purge=True)` on those events; `workspace_ttl` is the net
under everything nobody purged — an abandoned chat's workspace is swept once no
session has opened it for the configured time.

## Lifecycle ownership

> **`sandboxd` owns the sandbox lifecycle. AgenticOS owns only the session id.**

`AbstractCapability` has no teardown hook, and capabilities are built per run
inside `build_agent`. Nothing in AgenticOS is positioned to guarantee a sandbox
is released — so nothing in AgenticOS is made responsible for it.

`sandboxd` reaps on idle (`idle_timeout`, default 1800s), caps concurrency
(`max_sessions`, default 20, answering `429` beyond it) and now notifies its own
bookkeeping when a session is reaped. AgenticOS may call `stop()` from the
`finally` block in `agent_runner` as a courtesy for `session_scope="run"`, but
correctness must not rest on it.

The consequence to state in the Builder: with `session_scope="conversation"` and
no `workspace_root` configured, files live in the container's write layer, so an
idle reaping between turns discards them **silently** — the next turn opens a
fresh sandbox. `workspace_root` turns that into a host-backed volume per session
and makes the persistence real.

### Capacity

`session_scope="conversation"` holds a slot for as long as the thread is warm.
With the defaults, twenty concurrent conversations exhaust the pool and the
twenty-first user gets `429`. This is the second reason `"run"` is the default,
and the reason `max_sessions` is an operator-facing number rather than a
constant.

---

## Library changes

### Done

- **`CreateSessionRequest.reuse`** and the attach path in `open_session`.
  Previously a second `RemoteSandbox` naming an open session id got `409`, and
  `start()` turns any 4xx into `RuntimeError` — so `session_scope="conversation"`
  could not work at all. With `reuse`, the caller attaches and is handed the
  token the session already has. A `runtime` that disagrees with the open
  session is refused rather than honoured, because honouring it would mean
  replacing a live sandbox and discarding the files the caller came back for.
- **`SandboxdConfig.workspace_root`** mounts `{root}/{session_id}/workspace`
  into each sandbox, so files survive the container. Session ids are
  pattern-checked before they reach a path, so one cannot traverse out.
- **`SessionManager(on_release=...)`**, and `sandboxd` using it. Idle reaping
  dropped the sandbox but left the service's `_Session` record — its token and
  its 200-entry event log — behind for the life of the process, and `reuse`
  would have attached to a record with no sandbox under it.
- **`create_console_toolset(backend=...)` and `ConsoleCapability(backend=...)`.**
  The tools read `ctx.deps.backend` through the `ConsoleDeps` protocol, which no
  host with its own deps type can satisfy — `AgentDeps` has no `backend` field
  and should not grow one just for this. With an explicit backend the capability
  carries it, and the deps type stops mattering.
- **`ConsoleCapability(edit_format=...)` now reaches the toolset.** It only ever
  reached `get_instructions()`, so `edit_format="hashline"` produced instructions
  telling the model to call `hashline_edit` alongside a toolset registering
  `edit_file`.
- **`SandboxdConfig.persist_containers`** names each container after its session,
  so a reaped session is stopped rather than discarded and the next attach
  restarts the same filesystem. Without it, `workspace_root` preserves only the
  work directory and an agent reinstalls its packages after every idle timeout.
  Off by default, because stopped containers accumulate until a session is purged
  — which is also why it is a service setting rather than something a spec picks:
  an agent author would be choosing the host's disk consumption.
- **`DELETE /sessions/{id}?purge=true`** (and `RemoteSandbox.stop(purge=True)`)
  discards the container and the host workspace, for when the thing the session
  belonged to is gone. **`SandboxdConfig.workspace_ttl`** sweeps workspaces no
  session has opened for that long, on the same interval as idle reaping — the net
  under every conversation nobody explicitly closed.
- **A session opens on the first operation, not on construction.** `RemoteSandbox`
  performed a `POST /sessions` from `start()`, and AgenticOS would have called it
  while assembling the agent — so every run of an agent that *might* need a
  sandbox started a container, whether the model reached for one or not. Opening
  is now lazy and guarded, so two tool calls arriving together on the adapter's
  thread pool open one session instead of racing into a `409`. `start()` remains
  for pre-warming.
- **`/workspaces/{id}/ls` and `/workspaces/{id}/read`, plus `WorkspaceArchive`.**
  Read the host volume directly, so browsing a conversation from last week costs
  no container start and works for a session reaped long ago. Service token only —
  a reaped session has no token left, and the caller is an application applying
  its own authorization first. These raise rather than degrading, because no model
  is waiting on them and "there are no files" must be distinguishable from "the
  service is misconfigured". They are also the most safety-critical code in the
  module, since they read the host filesystem: a path resolving outside the
  workspace is refused whether it got there through `..` **or through a symlink
  the sandbox itself planted** (`ln -s /etc/shadow notes.txt` reads the host's
  file when followed from the host side).
- **Per-runtime ceilings** (`SandboxRuntime`), so AgenticOS can offer an author a
  choice between, say, a one-gigabyte shell and a four-gigabyte data runtime
  without the client ever naming a number — the alias still carries everything.
  `sandboxd` can now also serve runtimes whose packages are *built*, which is what
  makes `python-datascience` reachable from a spec rather than only images that
  already exist.
- **`SandboxdConfig.max_sessions_per_tenant`** with `CreateSessionRequest.tenant`.
  The label is declared by the client rather than parsed out of the session id, so
  the service imposes no naming convention; it is capacity accounting and grants
  nothing, since only the service token can open a session at all. Reported back
  in `GET /sessions` and `GET /policy`.

### Not needed for this integration

- `BaseSandbox.write` is annotated `str` while `BackendProtocol.write` accepts
  `str | bytes`. Correct binary write over a shell heredoc is new behaviour, not
  a refactor. The Docker, Daytona and remote backends all accept bytes already.

---

## AgenticOS changes

### 1. Dependency

`pydantic-ai-backend[remote]` in `backend/pyproject.toml`. The client only needs
`httpx`; `docker`, `pypdf` and `chardet` stay out of the app image because the
app never touches Docker.

### 2. The capability

`backend/app/agents/capabilities/sandbox/`, following the layout every other
capability uses (`__init__.py` registers, `_capability.py` holds the class,
`_toolset.py` holds the prompt surface):

```python
@register(
    id="sandbox",
    name="Files & shell",
    category="analysis",
    description="Read, write and run things in an isolated workspace.",
    tools=(...),                       # ls, read_file, write_file, edit_file, glob, grep, execute
    config_schema=SandboxConfig,
    scopes=("sandbox:execute",),
    side_effecting=True,
    secret=SecretRequirement(
        kind=SecretKind.API_KEY,
        description="Daytona API key",
        required_when=<backend == "daytona">,
    ),
)
def _build(ctx: CapabilityBuildContext) -> ConsoleCapability: ...
```

`required_when` is the same mechanism `web_research` uses for Tavily versus
DuckDuckGo: the Builder must ask for a key at exactly the moments the server
will demand one, and only `state` and `docker` need none.

The builder returns `ConsoleCapability(backend=..., permissions=...)` — the
library capability, constructed with the backend the config selected. Three
branches, one capability, because from the model's point of view this is one
decision ("does this agent have files and a shell"), not three.

### 3. Settings

Alongside the existing `LITEPARSE_OCR_SERVER_URL`, which is the same shape of
thing — an external sidecar this deployment may or may not run:

```python
SANDBOXD_URL: str = ""  # empty disables the docker backend
SANDBOXD_TOKEN: str = ""
```

With `SANDBOXD_URL` empty, publishing a spec that selects `backend="docker"`
must fail with a message naming the setting, not fail at run time inside a
conversation.

### 4. Plumbing the session id

`CapabilityBuildContext` carries `binding`, `config`, `resources` and `secret` —
no run or conversation id. Both are already in hand at the `build_agent` call
site in `agent_runner`, so they travel through `resources`, exactly as
`kb_collection_names` does. No new parameter on `build_agent`.

### 5. Compose

One service, and the socket mounted **only** there:

```yaml
sandboxd:
  image: pydantic-ai-backend-sandboxd
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock
    - sandbox_workspaces:/workspaces
  environment:
    SANDBOXD_TOKEN: ${SANDBOXD_TOKEN}
  networks: [backend]        # never published to the edge
```

`app` and `prefect-runner` reach it by service name over the internal network.
It gets no `ports:` entry: the service token can start containers on the host,
so a public listener is out of the question, and the same goes for `ui_enabled`.

### 6. Showing the files to the user

The session token unlocks `/exec` as well as `/ls` and `/read`, so it must never
reach a browser — a user with DevTools would have a shell in the sandbox. The
service token is worse still.

So AgenticOS proxies, read-only:

```
GET /api/conversations/{id}/files            -> sandboxd /ls
GET /api/conversations/{id}/files/{path}     -> sandboxd /read
```

The backend holds the token, checks access with `resolve_access` and forwards
only listing and reading, using `WorkspaceArchive` — which reads the stored
volume, so a week-old conversation lists its files without starting anything.
`WorkspaceArchiveError.status_code` carries the service's answer, so the route can
pass a real status through instead of showing an empty folder when the service is
misconfigured.

This is work AgenticOS has to own regardless: per-conversation authorization is
its knowledge, not `sandboxd`'s.

One UI consequence of the scope model: under `agent` or `user` scope, "this
conversation's files" is the wrong label — the files belong to the sandbox, not
the chat. Show whose environment it is, or a user will see files they never
created and read it as a leak.

### 7. Publish validation

Rejections, all in the spec validator so they surface in a form rather than
mid-conversation:

- `backend="state"` with any `session_scope` but `"run"` — cannot mean what it says.
- `backend="docker"` with no `SANDBOXD_URL` configured — nothing to talk to.
- `session_scope="user"` on a surface with no normalised user identity.
- `session_scope="agent"` without the `sandbox:shared` grant, if that scope is
  adopted.

---

## Test plan

- **Library.** Covered by `tests/test_remote_sandbox.py`: reuse attaches to an
  open session and returns its token; a colliding id without `reuse` is `409`; a
  disagreeing runtime is `409`; `reuse` with nothing open creates; a reaped
  session is forgotten and a later `reuse` opens a fresh sandbox;
  `workspace_root` yields one directory per session. `tests/test_capability.py`
  covers a capability-owned backend against deps with no `backend` attribute,
  and `edit_format` reaching the toolset.
- **AgenticOS.** Publish validation for both rejected combinations; the builder
  selecting each of the three backends; session id derivation for both scopes,
  including the length bound; the capability absent from the catalog when
  `sandbox:execute` is not granted.

## Open questions

1. **Does `agent` scope get its own permission scope?** `sandbox:shared` would
   make an organization consent to files being shared between its people, rather
   than an agent author deciding it alone. Recommended, not yet decided.
2. **Daytona session scope.** Daytona sandboxes are cloud resources with their
   own billing and lifecycle. `session_scope="conversation"` there means holding
   a paid resource across turns, and the reaping policy is Daytona's, not ours.
3. **Does the model need to know when files were lost?** After a reaping, the
   next turn gets an empty workspace with no explanation. With `workspace_root`
   and `persist_containers` configured this stops mattering; without them, the
   agent will confidently reference a file that is gone.
