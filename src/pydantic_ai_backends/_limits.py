"""Operational ceilings shared by more than one backend."""

from __future__ import annotations

MAX_EXECUTE_OUTPUT_BYTES = 100_000
"""Output retained from a single `execute()` call; the rest is discarded."""

DEFAULT_MAX_READ_BYTES = 8 * 1024 * 1024
"""Ceiling for a whole-file read.

`read_bytes` has to materialise the file in memory to satisfy its `bytes`
return type, so this is what stops one oversized file from exhausting the host.
"""

DEFAULT_READ_LIMIT = 2000
"""Line count a `read` returns when the caller asks for no specific range."""

READ_LIMIT_HINT = "Read a slice with execute() instead, e.g. \"sed -n '1,200p' <path>\"."
"""Appended to read-limit errors so the caller knows what to do instead."""
