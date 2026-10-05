"""The `sandboxd` service and what an application reads from it.

`sandboxd` (:mod:`pydantic_ai_backends.remote.server`, the `server` extra) owns
the Docker socket and rents out sandboxes over HTTP; an agent reaches one as a
Pydantic AI workspace through :class:`~pydantic_ai_backends.workspaces.SandboxdWorkspace`.

:class:`WorkspaceArchive` reads what sessions left behind without starting a
sandbox at all, so browsing an old conversation's files costs nothing.
"""

from pydantic_ai_backends.remote.archive import (
    WorkspaceArchive as WorkspaceArchive,
)
from pydantic_ai_backends.remote.archive import (
    WorkspaceArchiveError as WorkspaceArchiveError,
)
from pydantic_ai_backends.remote.wire import (
    SESSION_ID_PATTERN as SESSION_ID_PATTERN,
)
from pydantic_ai_backends.remote.wire import (
    TOKEN_HEADER as TOKEN_HEADER,
)

__all__ = [
    "SESSION_ID_PATTERN",
    "TOKEN_HEADER",
    "WorkspaceArchive",
    "WorkspaceArchiveError",
]
