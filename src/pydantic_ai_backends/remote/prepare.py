"""Make every sandboxd runtime image local, then exit - for a deploy to run first.

```bash
docker compose run --rm --no-deps sandboxd python -m pydantic_ai_backends.remote.prepare
```

sandboxd builds a runtime's image as it starts, in the background, and a session
that asks for one still being built waits for it. After a release that changed
a runtime that wait lands on whoever opens the first session: minutes, during
which every `POST /sessions` for it answers 409. Run in a one-off container of
the *new* image - same environment, same Docker socket - while the old service
keeps serving, this moves the build into the deploy, and the new service finds
every image in the cache.

It reads the same `SANDBOXD_*` variables the service does, so it prepares the
images the service will look for: a built image's tag is a hash of its runtime.

Exit status: 0 when every runtime is ready, 1 when one never became ready (the
service will still build it on demand), 2 when the configuration is invalid.
"""

from __future__ import annotations

import logging
import os
import sys

from pydantic_ai_backends.remote.env import SandboxdConfigError, config_from_env
from pydantic_ai_backends.remote.server import prepare_runtimes

ATTEMPTS = 3
"""Tries per runtime. A deploy can afford to wait; a session should not have to."""


def main() -> int:
    """Prepare the allowlist the environment describes. Returns the exit status."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        config = config_from_env(os.environ)
    except SandboxdConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    failed = prepare_runtimes(config, attempts=ATTEMPTS)
    if failed:
        print(
            f"Not prepared: {', '.join(failed)}. sandboxd will build them when a session "
            "asks, and that session waits until it has.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
