# CLAUDE.md

Guidance for Claude Code when working on this repository.

## What This Project Is

**pydantic-ai-backend** provides file storage and sandbox backends for AI agents. It's designed to work with pydantic-ai and pydantic-deep.

Key pattern: **Protocol-based backends** - all backends implement `BackendProtocol` for consistent file operations.

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
├── types.py          # FileData, FileInfo, WriteResult, EditResult, RuntimeConfig
├── protocol.py       # BackendProtocol, SandboxProtocol (+ async variants)
├── adapter.py        # Sync -> async adapters, ensure_async()
├── capability.py     # ConsoleCapability for pydantic-ai
├── hashline.py       # Content-hash line editing
├── _editing.py       # Shared `edit` replacement rules
├── _limits.py        # Output and read ceilings
├── _optional.py      # Optional-extra imports with install hints
├── _paths.py         # Virtual path normalisation and validation
├── _text.py          # Encoding detection, decoding, PDF extraction
├── backends/
│   ├── base.py       # BaseSandbox (shell-based defaults)
│   ├── state.py      # StateBackend (in-memory)
│   ├── local.py      # LocalBackend (real filesystem + shell)
│   ├── composite.py  # PrefixRouter, CompositeBackend, AsyncCompositeBackend
│   ├── daytona.py    # DaytonaSandbox
│   ├── kubernetes.py # KubernetesPodSandbox
│   ├── _background.py  # Long-lived process registry
│   ├── _guard.py     # Synchronous permission enforcement
│   └── docker/       # sandbox.py, session.py, runtimes.py, _client/_image/_stats
├── permissions/      # types.py, checker.py, presets.py
├── toolsets/         # console.py, descriptions.py, _content/_tracking/_ruleset
└── remote/           # client.py (RemoteSandbox), server.py (sandboxd), wire.py
```

Modules with a leading underscore are internal: no compatibility promise, and the
names inside them are public so call sites read cleanly.

## Core Pattern

```python
class BackendProtocol(Protocol):
    def ls_info(self, path: str) -> list[FileInfo]: ...
    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str: ...
    def write(self, path: str, content: str | bytes) -> WriteResult: ...
    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> EditResult: ...
    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]: ...
    def grep_raw(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> list[GrepMatch] | str: ...


class SandboxProtocol(BackendProtocol, Protocol):
    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse: ...
    @property
    def id(self) -> str: ...
```

## Requirements

- **100% test coverage** - every PR must maintain this
- **Type annotations** - pyright and mypy strict mode
- **Lazy loading** - optional deps (docker, pypdf, chardet) loaded on-demand

## Testing

```bash
# Run specific test
uv run pytest tests/test_backends.py::TestStateBackend -v

# Debug mode
uv run pytest -v -s
```

## Integration

This library is used by [pydantic-deep](https://github.com/vstorm-co/pydantic-deep) which re-exports its API. Changes here affect pydantic-deep users.
