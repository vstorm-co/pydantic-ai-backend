# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
