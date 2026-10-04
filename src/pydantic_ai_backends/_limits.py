"""Operational ceilings shared by more than one backend."""

from __future__ import annotations

MAX_EXECUTE_OUTPUT_BYTES = 100_000
"""Output retained from a single `execute()` call; the rest is discarded."""

MAX_RUN_OUTPUT_BYTES = 10 * 1024 * 1024
"""Combined stdout and stderr a `run_command` may produce before it is stopped.

Larger than `MAX_EXECUTE_OUTPUT_BYTES` because the reader is code, not a model,
and a command that outgrows it fails rather than being cut short silently. The
same ceiling as Pydantic AI's local workspace, so a command fails the same way
wherever it runs.
"""

DEFAULT_MAX_READ_BYTES = 8 * 1024 * 1024
"""Ceiling for a whole-file read.

`read_bytes` has to materialise the file in memory to satisfy its `bytes`
return type, so this is what stops one oversized file from exhausting the host.
"""

DEFAULT_READ_LIMIT = 2000
"""Line count a `read` returns when the caller asks for no specific range."""

READ_LIMIT_HINT = "Read a slice with execute() instead, e.g. \"sed -n '1,200p' <path>\"."
"""Appended to read-limit errors so the caller knows what to do instead."""
