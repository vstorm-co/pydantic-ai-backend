# CLAUDE.md

Guidance for Claude Code when working on this repository.

## What This Project Is

**pydantic-ai-backend** supplies Pydantic AI workspaces (Docker, sandboxd, Kubernetes,
Daytona, a JSON state document) and the console tools that work in any workspace. It's
designed to work with pydantic-ai and pydantic-deep.

Key pattern: **Pydantic AI workspaces** — tools reach the environment through
`ctx.workspace`; a workspace capability supplies it. This library has no file-operation
protocol of its own.

## Commands

```bash
uv sync --all-extras --group dev  # Install all dependencies
uv run pytest                      # Run tests
uv run coverage run -m pytest && uv run coverage report  # Test with coverage
uv run ruff check .                # Lint
uv run ruff format .               # Format
uv run pyright                     # Type check
uv run mypy src/pydantic_ai_backends  # MyPy check
```

## Structure

```
src/pydantic_ai_backends/
├── __init__.py       # Public API, lazily loaded
├── types.py          # FileData, FileInfo, CommandOutcome, RuntimeConfig, results
├── protocol.py       # CommandRunner, SandboxUnavailableError
├── capability.py     # ConsoleCapability for pydantic-ai
├── hashline.py       # Content-hash line editing
├── _editing.py       # Shared `edit` replacement rules
├── _limits.py        # Output and read ceilings
├── _optional.py      # Optional-extra imports with install hints
├── _text.py          # Encoding detection, decoding, PDF extraction
├── backends/         # The sandboxes the workspaces run on
│   ├── _runner.py    # The pid-file wrapper and stopper every runner shares
│   ├── state.py      # StateBackend (a filesystem as a JSON document)
│   ├── kubernetes.py # KubernetesPodSandbox (pods/exec)
│   └── docker/       # sandbox.py, session.py, runtimes.py, _exec/_client/_image/_stats
├── workspaces/       # Docker/Sandboxd/Kubernetes/Daytona/StateWorkspace (+ *Backend)
├── permissions/      # types.py, checker.py, presets.py
├── toolsets/         # console.py, descriptions.py, _workspace (ops over ctx.workspace),
│                     # _guard, _shell (glob/grep), _content/_tracking/_ruleset/_failures
└── remote/           # server.py (sandboxd), wire.py, archive.py, env.py, ui/
```

Modules with a leading underscore are internal: no compatibility promise, and the
names inside them are public so call sites read cleanly.

## Core Pattern

```python
class CommandRunner(Protocol):
    async def run_command(
        self,
        argv: Sequence[str],
        *,
        run_id: str,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        output_limit: int | None = None,
    ) -> CommandOutcome: ...
    async def stop_command(self, run_id: str) -> None: ...
```

A sandbox that implements `CommandRunner` can back a container workspace and a sandboxd
session. File operations are derived by Pydantic AI's `Workspace` through the shell;
only `StateWorkspaceBackend` implements `SupportsFilesystem` natively. Every workspace
here must pass `pydantic_ai.workspaces.conformance.WorkspaceBackendSuite`.

## Requirements

- **100% test coverage** - every PR must maintain this
- **Type annotations** - pyright and mypy strict mode
- **Lazy loading** - optional deps (docker, pypdf, chardet) loaded on-demand

## Testing

```bash
# Run specific test
uv run pytest tests/test_state.py -v

# Against a real Docker daemon
uv run pytest -m docker

# Debug mode
uv run pytest -v -s
```

## Integration

This library is used by [pydantic-deep](https://github.com/vstorm-co/pydantic-deepagents) which re-exports its API. Changes here affect pydantic-deep users.
