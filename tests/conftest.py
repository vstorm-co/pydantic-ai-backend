"""Collection rules shared by the whole suite."""

from __future__ import annotations

import importlib.util

# Pydantic AI workspaces arrived in 2.52, and the `console` extra still declares
# 1.74 as its floor - CI installs that floor to prove the rest of the library
# runs on it. The workspace tests import `pydantic_ai.workspaces` at the top, so
# on an older release they are left uncollected rather than failing to import.
_HAS_WORKSPACES = importlib.util.find_spec("pydantic_ai.workspaces") is not None
collect_ignore_glob = [] if _HAS_WORKSPACES else ["test_workspace_*.py"]
