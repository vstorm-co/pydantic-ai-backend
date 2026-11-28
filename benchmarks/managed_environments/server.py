"""Own sandboxd benchmark service; imported from an installed local wheel only."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path

import uvicorn

from pydantic_ai_backends.remote.server import SandboxdConfig, SandboxRuntime, create_app
from pydantic_ai_backends.types import RuntimeConfig


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--browser-image", required=True)
    parser.add_argument("--desktop-image", required=True)
    parser.add_argument("--browser-network", default="none")
    parser.add_argument("--browser-proxy", default="http://127.0.0.1:9")
    args = parser.parse_args()
    args.folder.mkdir(parents=True, exist_ok=True)
    args.folder.chmod(0o700)
    if (args.folder / "service-token").exists():
        raise ValueError("Choose a new output folder instead of overwriting an existing credential")
    (args.folder / "server-pid").write_text(str(os.getpid()))
    token = secrets.token_urlsafe(32)
    private = args.folder / "service-token"
    descriptor = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as credential:
        credential.write(token)
    workspace = args.folder / "workspaces"
    workspace.mkdir(exist_ok=True)
    workspace.chmod(0o777)
    config = SandboxdConfig(
        token=token,
        runtimes={
            "browser": SandboxRuntime(
                runtime=RuntimeConfig(
                    name="browser",
                    image=args.browser_image,
                    env_vars={"SANDBOXD_BROWSER_PROXY": args.browser_proxy},
                ),
                mem_limit="1g",
                cpus=1,
                network_mode=args.browser_network,
                pids_limit=256,
            ),
            "desktop": SandboxRuntime(
                image=args.desktop_image,
                mem_limit="1g",
                cpus=1,
                network_mode="none",
                pids_limit=256,
            ),
        },
        default_runtime="browser",
        prewarm=False,
        workspace_root=str(workspace),
        max_sessions=2,
        max_open_sessions=4,
        max_sessions_per_tenant=2,
        network_mode="none",
        idle_timeout=900,
        cleanup_interval=1,
        persist_containers=True,
        ui_enabled=False,
    )
    import pydantic_ai_backends

    if "/site-packages/" not in str(Path(pydantic_ai_backends.__file__).resolve()):
        raise RuntimeError("The benchmark service requires an installed wheel")

    (args.folder / "installed-import.json").write_text(
        json.dumps(
            {
                "package": str(Path(pydantic_ai_backends.__file__).resolve()),
                "version": pydantic_ai_backends.__version__,
                "port": args.port,
                "network": args.browser_network,
                "proxy_configured": bool(args.browser_proxy),
            },
            indent=2,
        )
    )
    uvicorn.run(create_app(config), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
