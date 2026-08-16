"""Timestamp rendering shared by the filesystem-backed listings.

One function so `FileInfo.modified_at` is the same shape everywhere it is
filled from a stat: ISO 8601, UTC, matching what `StateBackend` records on
write.
"""

from __future__ import annotations

from datetime import datetime, timezone


def iso_mtime(epoch: float) -> str:
    """Render a `st_mtime` epoch as an ISO 8601 UTC timestamp."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
