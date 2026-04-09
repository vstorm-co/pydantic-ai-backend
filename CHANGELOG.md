# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
