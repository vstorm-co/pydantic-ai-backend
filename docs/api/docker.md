# Docker API

## DockerSandbox

::: pydantic_ai_backends.backends.docker.sandbox.DockerSandbox
    options:
      show_root_heading: true
      members:
        - __init__
        - runtime
        - session_id
        - execute
        - read
        - write
        - ls_info
        - glob_info
        - grep_raw
        - start
        - stop
        - is_alive

## BaseSandbox

::: pydantic_ai_backends.backends.docker.sandbox.BaseSandbox
    options:
      show_root_heading: true
      members:
        - __init__
        - id
        - execute
        - ls_info
        - read
        - write
        - edit
        - glob_info
        - grep_raw

## SessionManager

::: pydantic_ai_backends.backends.docker.session.SessionManager
    options:
      show_root_heading: true
      members:
        - __init__
        - get_or_create
        - release
        - cleanup_idle
        - start_cleanup_loop
        - shutdown
        - sessions
        - session_count

## RuntimeConfig

The runtime descriptor (image, setup commands, environment) used by
[`DockerSandbox`][pydantic_ai_backends.DockerSandbox] and the session manager is
documented in the type reference: [`RuntimeConfig`][pydantic_ai_backends.types.RuntimeConfig].

## Built-in Runtimes

```python
from pydantic_ai_backends import BUILTIN_RUNTIMES

# Available runtimes
print(sorted(BUILTIN_RUNTIMES))

# Use a runtime
from pydantic_ai_backends import DockerSandbox

sandbox = DockerSandbox(runtime="python-datascience")
```

| Runtime | Image | What it adds |
|---|---|---|
| `python-minimal` | python:3.12-slim | standard library only |
| `python-datascience` | built on python:3.12-slim | pandas, numpy, matplotlib, scikit-learn, seaborn |
| `python-analytics` | built on python:3.12-slim | duckdb, polars, pyarrow |
| `python-web` | built on python:3.12-slim | fastapi, uvicorn, sqlalchemy, httpx |
| `python-scraping` | built on python:3.12-slim | httpx, beautifulsoup4, lxml, markdownify |
| `python-documents` | built on python:3.12-slim | pypdf, python-docx, openpyxl, pillow |
| `node-minimal` | node:20-slim | nothing |
| `node-typescript` | built on node:20-slim | typescript, tsx, vitest |
| `node-react` | built on node:20-slim | typescript, vite, react, react-dom, @types/react |
| `bun` | oven/bun:1-slim | Bun's own bundler, test runner and package manager |
| `deno` | denoland/deno:alpine | TypeScript with no install step |
| `go` | golang:1.23-alpine | Go toolchain |
| `rust` | rust:1-slim | Rust toolchain with cargo |

A runtime naming an `image` starts as fast as a pull. One naming a `base_image`
plus `packages` builds an image on first use and hits the cache afterwards, which
is worth it when installing them per session would dominate.
