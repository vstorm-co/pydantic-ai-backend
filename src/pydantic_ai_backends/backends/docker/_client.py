"""The process-wide Docker client.

`docker.from_env()` negotiates the API version with a blocking `GET /version`
and builds a `requests.Session` with its own connection pool. One client per
sandbox therefore cost a daemon round trip per session and pinned a socket pool
for as long as the container object lived, so a single client is shared.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

from pydantic_ai_backends._optional import load

if TYPE_CHECKING:
    from docker import DockerClient

_lock = threading.Lock()
_client: DockerClient | None = None
_client_pid: int | None = None


def docker_client() -> DockerClient:
    """Return the shared Docker client, creating it on first use.

    The client is rebuilt after a fork: its pooled sockets must not be used
    from two processes at once — the replies interleave — and web and task
    servers routinely fork workers once the module is already imported.

    Raises:
        ImportError: If the optional `docker` package is not installed.
    """
    global _client, _client_pid

    pid = os.getpid()
    with _lock:
        client = _client
        if client is None or _client_pid != pid:
            client = load("docker", purpose="DockerSandbox").from_env()
            _client = client
            _client_pid = pid
        return client
