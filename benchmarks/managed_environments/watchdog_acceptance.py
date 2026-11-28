"""Kill this fixture's sandboxd process; observe the browser's independent deadline."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import signal
import socket
import sys
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import docker
import httpx
from proxy_acceptance import wait_ready

from pydantic_ai_backends.browser import SandboxdBrowser
from pydantic_ai_backends.remote.wire import SessionCreated


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--browser-image", required=True)
    parser.add_argument("--desktop-image", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--stop-chromium-before-service-kill", action="store_true")
    args = parser.parse_args()
    args.folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    sid = "qa-watchdog-" + uuid4().hex[:10]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    base = "http://127.0.0.1:" + str(port)
    report: dict[str, object] = {
        "wheel": args.wheel.name,
        "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        "scope": "Own sandboxd SIGKILL, not a client disconnect",
        "model_calls": 0,
    }
    process = None
    connection = None
    try:
        with (args.folder / "server.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).with_name("server.py")),
                "--folder",
                str(args.folder),
                "--port",
                str(port),
                "--browser-image",
                args.browser_image,
                "--desktop-image",
                args.desktop_image,
                *(["--persist-browser-profiles"] if args.stop_chromium_before_service_kill else []),
                stdout=log,
                stderr=log,
            )
        async with httpx.AsyncClient(base_url=base, timeout=30) as client:
            await wait_ready(client)
            response = await client.post(
                "/sessions",
                headers={"X-Sandbox-Token": (args.folder / "service-token").read_text()},
                json={"session_id": sid, "runtime": "browser", "tenant": sid},
            )
            response.raise_for_status()
            session = SessionCreated.model_validate_json(response.content)
        api = SandboxdBrowser(base, sid, session.token)
        if args.stop_chromium_before_service_kill:
            await api.start(
                idle_seconds=900,
                lifetime_seconds=8,
                profile_key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
            )
            report["checkpoint_interruption_scope"] = (
                "Manually reproduce checkpoint writer SIGSTOP on exact Chromium group; "
                "Docker/watchdog remain running, then kill own service"
            )
        else:
            await api.start(idle_seconds=900, lifetime_seconds=8)
        connection = await api.connect()
        tab = await connection.browser.new_tab()
        await tab.evaluate("document.body.textContent='supervisor crash acceptance'")
        with closing(docker.from_env()) as docker_client:
            container = docker_client.containers.get("sandboxd-" + sid)
            state = container.exec_run(["cat", "/tmp/sandboxd-browser-watchdog.json"])
            assert state.exit_code == 0
            initial = json.loads(state.output)
            await connection.aclose()
            connection = await api.connect()
            after = container.exec_run(["cat", "/tmp/sandboxd-browser-watchdog.json"])
            assert after.exit_code == 0
            assert json.loads(after.output)["deadline"] == initial["deadline"]
            remaining = container.exec_run(
                [
                    "python3",
                    "-c",
                    "import json,time; from pathlib import Path; "
                    "s=json.loads(Path('/tmp/sandboxd-browser-watchdog.json').read_text()); "
                    "print(s['deadline']-time.monotonic())",
                ]
            )
            assert remaining.exit_code == 0
            remaining_seconds = float(remaining.output)
            original_deadline_estimate = time.monotonic() + remaining_seconds
            version = container.exec_run(
                [
                    "python3",
                    "-c",
                    "import importlib.metadata; "
                    "print(importlib.metadata.version('websocket-client'))",
                ]
            )
            assert version.exit_code == 0
            report["runtime_websocket_client_version"] = version.output.decode().strip()
            report["runtime_image_id"] = container.image.id
            assert 0 < remaining_seconds <= 8
            if args.stop_chromium_before_service_kill:
                paused = container.exec_run(
                    [
                        "python3",
                        "-c",
                        "import json,os,signal,time; from pathlib import Path; "
                        "s=json.loads(Path('/tmp/sandboxd-browser-watchdog.json').read_text()); "
                        "pid=s['browser']['pid']; p=Path('/proc')/str(pid); "
                        "raw=(p/'stat').read_text(); fields=raw[raw.rindex(')')+2:].split(); "
                        "assert int(fields[19])==s['browser']['start']; "
                        "assert (p/'exe').resolve().name=='chromium'; "
                        "assert os.getpgid(pid)==pid and pid>1; "
                        "os.killpg(pid,signal.SIGSTOP); time.sleep(0.05); "
                        "raw=(p/'stat').read_text(); "
                        "assert raw[raw.rindex(')')+2:].split()[0] in ('T','t'); "
                        "w=Path('/proc')/str(s['watchdog']['pid'])/'stat'; raw=w.read_text(); "
                        "assert raw[raw.rindex(')')+2:].split()[0] not in ('T','t'); "
                        "print('exact Chrome group stopped; independent watchdog still runs')",
                    ]
                )
                assert paused.exit_code == 0
                container.reload()
                assert container.attrs["State"]["Paused"] is False
                report["chromium_group_stopped_before_supervisor_kill"] = True
                report["docker_container_paused"] = False
                report["watchdog_process_stopped"] = False
            killed_at = time.monotonic()
            process.send_signal(signal.SIGKILL)
            assert await process.wait() == -signal.SIGKILL
            deadline = original_deadline_estimate
            while time.monotonic() < deadline + 3:
                container.reload()
                if container.status != "running":
                    break
                await asyncio.sleep(0.05)
            stopped_at = time.monotonic()
            assert container.status != "running", "Browser survived its independent hard deadline"
            assert stopped_at >= deadline - 0.25, "Browser stopped before its hard deadline"
            report.update(
                {
                    "status": "passed",
                    "reconnect_preserved_original_deadline": True,
                    "supervisor_exit_code": process.returncode,
                    "remaining_lifetime_at_kill_seconds": deadline - killed_at,
                    "deadline_observation_tolerance_seconds": 0.25,
                    "container_stop_after_supervisor_kill_seconds": stopped_at - killed_at,
                    "container_stop_after_original_deadline_seconds": stopped_at - deadline,
                    "container_status": container.status,
                }
            )
    except BaseException as error:
        report.update(
            {"status": "failed", "failure_type": type(error).__name__, "failure": str(error)[:1000]}
        )
        raise
    finally:
        if connection is not None:
            try:
                await asyncio.wait_for(connection.aclose(), timeout=3)
            except Exception as error:
                report["detach_after_service_kill"] = type(error).__name__
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        with closing(docker.from_env()) as docker_client:
            try:
                container = docker_client.containers.get("sandboxd-" + sid)
                if container.name != "sandboxd-" + sid:
                    raise RuntimeError("Refusing cleanup of a foreign container")
                container.remove(force=True)
            except docker.errors.NotFound:
                pass
        credential = args.folder / "service-token"
        if credential.exists():
            credential.unlink()
        (args.folder / "receipt.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    asyncio.run(main())
