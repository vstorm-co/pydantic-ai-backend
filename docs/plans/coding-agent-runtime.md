# The runtime a coding agent should get

Research note, August 2026. The question asked was: what belongs in a Docker
environment built for coding agents, and how much further can it be optimised?

Unlike the two notes beside this one, **the numbers here are measured**, on
Docker 29.2.1 with `python:3.12-slim`. Sizes and pass/fail are
architecture-independent; the timings are from an ARM laptop and are useful as
ratios rather than absolutes. Nothing here is quoted from a vendor.

## Two things are broken before any tuning

Both were found by running the sandbox the way an agent uses it, and both hit
the tool a coding agent reaches for most.

### Every orphaned process becomes a permanent zombie

Our containers run `sleep infinity` as PID 1. An orphaned process is reparented
to PID 1, which is expected to `wait()` on it. `sleep` never does. Measured, ten
orphans in one container:

| | zombies | processes in container |
|---|---|---|
| as we run it today | **10** | 13 |
| same, with `init=True` | 0 | 4 |

They never go away, and `pids_limit` defaults to 512. An agent backgrounds
things constantly — `npm run dev &`, a test server, anything killed by the
`timeout` wrapper — and `DockerSandbox` has no background-process registry, so
`cmd &` inside a shell command is the *only* way to do it. Every one of those
leaves a zombie behind when it dies.

The session does not fail visibly. It accumulates until a `fork` fails, and then
every command in that session returns `Resource temporarily unavailable`. The
hibernate work makes this worse rather than better: sessions now live across
many more turns than they used to.

The fix is one argument — Docker's `--init` inserts `tini` as PID 1 — and it is
the highest-value line in this note.

### git refuses to work in the workspace

`workspace_root` bind-mounts a host directory into the container, and the
container runs as root. Git compares the repository's owner against the current
user and refuses when they differ. Measured, a repository owned by uid 1000 with
git running as root — exactly our shape:

```
fatal: detected dubious ownership in repository at '/ws'
```

Every git command. `status`, `diff`, `log`, `commit`, `add`. And once that is
fixed, the next one lands:

```
Author identity unknown
*** Please tell me who you are.
```

An agent asked to commit its work cannot. Two `git config --system` lines in the
image close both, and a third and fourth are worth having beside them:

```dockerfile
RUN git config --system --add safe.directory '*' \
 && git config --system user.name  'Agent' \
 && git config --system user.email 'agent@sandbox.local' \
 && git config --system init.defaultBranch main \
 && git config --system advice.detachedHead false
```

`safe.directory '*'` is the right call *here* specifically because the boundary
is the container, not the uid: everything inside is already the same trust
domain, and the check is protecting against a threat model we do not have.

## What to install, and what each thing costs

Measured as the added layer over `python:3.12-slim`, which is 41.5 MB itself:

| | added | verdict |
|---|---|---|
| `git` | **33.1 MB** | non-negotiable, and the single largest line |
| `curl` + `ca-certificates` | +0.7 MB | needed for the network runtimes |
| `ripgrep` | 1.9 MB | yes |
| `fd-find` | 1.4 MB | yes |
| `jq` | 0.4 MB | yes |
| `less` | 0.2 MB | yes |
| `procps` (`ps`, `kill`) | 0.4 MB | yes |
| `uv` (binary `COPY --from`) | 19.9 MB | yes — see below |
| `uv` (via `pip install`) | 23.8 MB | no, use the binary |
| `nodejs` + `npm` (Debian) | 64.6 MB | only where JavaScript is the job |
| `build-essential` | 94.1 MB | no — see below |

The striking number is the middle block: **ripgrep, fd, jq, less and procps
together cost 4.3 MB.** They are the tools an agent uses to *look* at a
codebase, and leaving them out to save four megabytes is a bad trade at any
image size. Without `rg` an agent falls back to `grep -r`, which on a large tree
is slower by an order of magnitude and floods the model's context with matches
from `node_modules` and `.git`.

`ps` and `kill` deserve their own mention: without `procps` an agent that starts
a background server has no way to see it or stop it. That is 0.4 MB to make
`cmd &` a usable pattern rather than a one-way door.

**`build-essential` is the one to refuse.** 94 MB — more than doubling a minimal
runtime — to compile the wheels that manylinux already ships prebuilt. It earns
its place only in a runtime whose whole purpose is building native extensions,
and it should be a separate alias rather than a default. This is also the reason
the base stays `python:3.12-slim` rather than Alpine: on musl, a package without
a musllinux wheel builds from source, which
[turns a 13-second install into over four minutes](https://pythonspeed.com/articles/alpine-docker-python/).
Alpine saves ~30 MB and costs minutes per build; it is the wrong direction.

## `uv`, and the surprise in it

An agent installs packages at runtime constantly — it writes a script, finds an
import missing, installs it. That cost is paid inside somebody's turn, so it is
worth optimising. Measured inside the container, cold cache:

| | pip | uv | |
|---|---|---|---|
| `pandas` | 8485 ms | **1169 ms** | 7.3× |
| `httpx rich` | 2495 ms | **487 ms** | 5.1× |

Then the surprise, testing the same install under a memory ceiling:

| ceiling | pip | uv (default) | uv, concurrency 2 |
|---|---|---|---|
| 256 MB | OK | OK | OK |
| 192 MB | OK | OK | OK |
| 128 MB | OK | **killed, OOM** | OK |

**uv is not strictly better than pip on a small sandbox.** It downloads and
unpacks in parallel, and that parallelism is memory. At 128 MB it is killed
where pip survives. Capping it fixes that and costs almost nothing — 1279 ms
against 1169 ms unrestricted, still 6.6× faster than pip:

```dockerfile
ENV UV_CONCURRENT_DOWNLOADS=2 UV_CONCURRENT_INSTALLS=2
```

That is the kind of default that only shows up if you test at the ceiling you
actually run at. Ship uv, but never uncapped.

## Environment, which has to be baked in

`DockerSandbox.execute` calls `exec_run(argv, workdir=...)` with no
`environment=`, so there is nowhere to set a variable per command. Everything
below has to be `ENV` in the image:

```dockerfile
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1 \
    PIP_ROOT_USER_ACTION=ignore \
    UV_SYSTEM_PYTHON=1 \
    UV_CONCURRENT_DOWNLOADS=2 \
    UV_CONCURRENT_INSTALLS=2 \
    DEBIAN_FRONTEND=noninteractive \
    GIT_TERMINAL_PROMPT=0 \
    NO_COLOR=1 \
    PAGER=cat \
    GIT_PAGER=cat \
    LANG=C.UTF-8
```

Why each earns its line:

- **`PYTHONUNBUFFERED`** — without it a script's `print` output sits in a pipe
  buffer, so a command killed by `timeout` returns *nothing* instead of the
  output up to the point it hung. That is the difference between an agent
  debugging a slow test and an agent staring at an empty string.
- **`NO_COLOR`, `PAGER`, `GIT_PAGER`** — ANSI escapes are tokens the model pays
  for and cannot read. There is no TTY here so most tools already behave, but
  the ones that force colour anyway are pure waste.
- **`PIP_ROOT_USER_ACTION=ignore`** and **`PIP_DISABLE_PIP_VERSION_CHECK`** —
  each suppresses a warning that would otherwise be prepended to every install
  an agent ever runs.
- **`LANG=C.UTF-8`** — `python:3.12-slim` sets this; **`node:20-slim` does
  not** (measured: unset). A Node runtime therefore starts in the POSIX locale,
  where non-ASCII filenames and output are a coin flip. Worth setting explicitly
  on every base rather than relying on the one that happens to.
- **`GIT_TERMINAL_PROMPT=0`** — this one is *cosmetic*, and the note it replaces
  was wrong. A clone of a private repository does not hang: with no TTY, git
  fails immediately with `could not read Username`. Setting it only makes the
  error say `terminal prompts disabled`, which is a clearer thing for a model to
  read. Keep it, but not for the reason one would assume.

## Compile bytecode at build time, not at run time

Ordinarily `pip install --no-compile` is a reasonable trade: installs get
faster, and the `.pyc` is generated on first import instead. For a sandbox image
it is exactly backwards, for a reason specific to running many sessions at once.

A `.pyc` written during the build lands in a **read-only image layer**, which
every container off that image shares — one copy of the compiled standard
library in the host's page cache, however many sessions are open. A `.pyc`
written at run time lands in each container's **private write layer**: compiled
again per session, cached again per session, and growing the disk each time.

So on this path the build-time cost is paid once and the runtime saving is
multiplied by the session count. Keep pip's default (it compiles), and where uv
is used for the build, set `UV_COMPILE_BYTECODE=1` — uv does not compile by
default.

## What is still open

- ~~**Running as a non-root user.**~~ Shipped as `SandboxdConfig.sandbox_uid`
  and `RuntimeConfig.run_as_uid`. The measurements changed the design twice on
  the way, and both corrections are worth keeping:

  - **`getpwuid` was not the problem.** The note below guessed git would break
    on a uid with no `/etc/passwd` entry. It does not — git works fine. What
    breaks is `pip`, on `Permission denied: '/.local'`, because `HOME` defaults
    to `/`.
  - **`uv` was the real obstacle**, and it is not fixable with a writable home.
    `uv` has no `--user` mode: `--system` hits permission denied on the
    interpreter's `site-packages`, and without it there is simply no
    environment. A virtualenv the sandbox user owns is the only design that
    works, which is why option (1) below was not the one taken.
  - **The container's environment overrides its image's**, so the
    `UV_SYSTEM_PYTHON=0` the image asks for was being clobbered by the sandbox
    default. It is dropped for an unprivileged runtime instead.

  Validated on true Linux filesystem semantics rather than through a macOS bind
  mount: `whoami`, `git commit`, `pip install`, `uv pip install` and running the
  installed console script all succeed, while `/etc` and the system
  `site-packages` are refused.

  Original reasoning, kept because the options were the argument:

  `_run_kwargs` set no `user`, so every sandbox ran as uid 0. Two things
  followed: an escape starts from root rather than from nobody, and every file
  the agent writes into the bind-mounted workspace is owned by root *on the
  host*, so a `sandboxd` running unprivileged cannot clean up after its own
  sessions. `no-new-privileges` does not help — it stops a process gaining
  privileges it lacks, and this one already has them.

  It is not a one-line fix, because adding `user="1000:1000"` breaks the happy
  path immediately: `_session_volumes` creates
  `{workspace_root}/{id}/workspace` as whatever uid `sandboxd` runs as, and a
  container user who does not own it cannot write there. Trading "an escape
  starts from root" for "no agent can save a file" is a bad trade.

  Three ways out, in the order they are worth considering:

  1. **Match the uid to the directory.** `os.stat` the session's workspace and
     pass `user=f"{st_uid}:{st_gid}"`. Docker accepts a numeric id with no
     `/etc/passwd` entry, so no image change is needed — but tools that call
     `getpwuid` then find nothing, and git and pip both do. Needs `HOME` set
     explicitly, and needs testing rather than assuming.
  2. **`chown` the workspace to a fixed sandbox uid** when it is created. Moves
     the problem to the host side, where `sandboxd` may not have the privilege
     to do it.
  3. **`userns-remap` on the daemon**, which makes container-root an
     unprivileged host user. The strongest boundary and the only one that needs
     no code, but it is daemon-wide, affects every container on the host, and
     shifts bind-mount ownership in its own way.

  (1) is the one to try, and it belongs in its own change with its own tests.
- ~~**`--init`'s cost.**~~ Settled: `docker-init` is **488 kB RSS**, about 2% of
  what an idle session costs. Most of that is a static binary shared with every
  other container off the host's page cache, so the marginal figure is lower
  still. Against a session that eventually cannot fork, this is not a trade.
- **Whether `node` belongs in `polyglot`** — narrower than it first looked. The
  version objection does not hold: the base is Debian 13 trixie, which ships
  Node **20.19.2** against `node:20-slim`'s 20.20.2, so an agent gets a current
  LTS either way. Only npm is behind (9.2.0 against 10.x). What remains is 64.6
  MB of disk and build time — and *not* memory, since pages nobody reads are
  never cached. That makes the case for removing it weak, and the question
  reduces to whether the generalist runtime should build slower for a language
  half its users will not touch.

## The proposal

One new built-in runtime — call it `coding` — and it should probably become the
default for agent work. Measured: **99.2 MB, 11 seconds to build.**

```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      git curl ca-certificates ripgrep fd-find jq less procps \
 && ln -s /usr/bin/fdfind /usr/local/bin/fd \
 && rm -rf /var/lib/apt/lists/*
RUN git config --system --add safe.directory '*' \
 && git config --system user.name  'Agent' \
 && git config --system user.email 'agent@sandbox.local' \
 && git config --system init.defaultBranch main \
 && git config --system advice.detachedHead false
ENV ...   # the block above
WORKDIR /workspace
```

Smoke-tested in the shape the service actually runs — root, a workspace owned by
another uid, `--init` on: the agent commits successfully, `rg`, `fd`, `jq` and
`uv` all answer, and the process table ends clean at zero zombies.

`ln -s /usr/bin/fdfind /usr/local/bin/fd` is not cosmetic: Debian ships the
binary as `fdfind` because of a name clash, and a model that has read the `fd`
documentation will type `fd`.

### Changes this implies in the codebase

1. **`init=True` on every container.** One line in `_run_kwargs`, and the only
   thing here that fixes a session which currently dies.
2. **The `coding` runtime**, in `BUILTIN_RUNTIMES` and mirrored into
   `SUGGESTED_RUNTIMES` — the second being what `sandboxd` hands out.
3. **The git and environment defaults**, which want somewhere to live. Every
   built-in runtime needs them, not just the new one, so they belong in the
   generated Dockerfile rather than in one runtime's `setup_commands`.
4. **`LANG=C.UTF-8` on the Node runtimes**, which do not get it from their base.
5. **`UV_COMPILE_BYTECODE=1`** wherever uv installs during a build.

Items 1 and 3 are bug fixes wearing a feature's clothes: a coding agent cannot
use git in our sandbox today, and a long-lived session runs out of processes.

## Sources

Measurements are our own. The two claims taken from elsewhere:

- [Alpine and Python wheels](https://pythonspeed.com/articles/alpine-docker-python/) —
  why the base stays Debian slim.
- [tini and PID 1](https://github.com/krallin/tini) — what `--init` inserts, and
  why reaping is its job.
