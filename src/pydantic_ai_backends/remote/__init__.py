"""Remote sandboxes over HTTP.

`RemoteSandbox` lets an application use sandboxes that live in another process,
so the application itself never needs Docker access. The service on the other
end is `sandboxd` (:mod:`pydantic_ai_backends.remote.server`), which requires
the `server` extra; the client only needs `httpx`.

```python
from pydantic_ai_backends.remote import RemoteSandbox

sandbox = RemoteSandbox("http://sandboxd:8080", token="...")
print(sandbox.execute("echo hi").output)   # the session opens on first use
sandbox.stop()
```

:class:`WorkspaceArchive` reads what sessions left behind without starting a
sandbox at all, so browsing an old conversation's files costs nothing.
"""

from pydantic_ai_backends.remote.client import (
    RemoteSandbox as RemoteSandbox,
)
from pydantic_ai_backends.remote.client import (
    WorkspaceArchive as WorkspaceArchive,
)
from pydantic_ai_backends.remote.client import (
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
    "RemoteSandbox",
    "WorkspaceArchive",
    "WorkspaceArchiveError",
]
