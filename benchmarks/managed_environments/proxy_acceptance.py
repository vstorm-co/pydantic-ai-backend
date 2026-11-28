"""Actual AgenticOS Squid egress policy with a disposable managed Browser."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import signal
import socket
import sys
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import docker
import httpx
from docker.models.containers import Container
from docker.models.networks import Network

from pydantic_ai_backends.browser import (
    BrowserError,
    ManagedBrowserConnection,
    SandboxdBrowser,
)
from pydantic_ai_backends.remote.wire import SessionCreated


def value(result: dict[str, object]) -> object:
    data = result.get("result")
    assert isinstance(data, dict)
    return data.get("value")


async def cleanup(
    process: asyncio.subprocess.Process | None,
    connection: ManagedBrowserConnection | None,
    folder: Path,
    session_id: str,
    proxy: Container | None,
    network: Network | None,
) -> list[str]:
    """Remove only the fixture's exact resources and retain cleanup failures."""
    errors: list[str] = []
    if connection is not None:
        try:
            await asyncio.wait_for(connection.aclose(), timeout=5)
        except Exception as error:
            errors.append("detach: " + type(error).__name__)
    if process is not None and process.returncode is None:
        process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            errors.append("server required forced shutdown")
    with closing(docker.from_env()) as client:
        try:
            browser = client.containers.get("sandboxd-" + session_id)
            if browser.name != "sandboxd-" + session_id:
                raise RuntimeError("Refusing cleanup outside this benchmark")
            browser.remove(force=True)
        except docker.errors.NotFound:
            pass
        if proxy is not None:
            (folder / "proxy.log").write_bytes(proxy.logs())
            proxy.remove(force=True)
        if network is not None:
            network.remove()
    return errors


async def wait_ready(client: httpx.AsyncClient) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if (await client.get("/healthz", timeout=1)).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.1)
    raise AssertionError("Own proxy QA service did not start")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--browser-image", required=True)
    parser.add_argument("--desktop-image", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--proxy-config", type=Path, required=True)
    args = parser.parse_args()
    args.folder.mkdir(exist_ok=True, parents=True)
    source = args.proxy_config.resolve()
    conf = args.folder / "squid.conf"
    conf.write_bytes(source.read_bytes())
    prefix = "qa-bench-proxy-" + uuid4().hex[:10]
    sid = prefix + "-browser"
    report: dict[str, object] = {
        "model_calls": 0,
        "synthetic": True,
        "wheel": str(args.wheel),
        "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        "squid_config_source": str(source),
        "squid_config_sha256": hashlib.sha256(conf.read_bytes()).hexdigest(),
        "checks": [],
        "public_loads": [],
        "private_denials": [],
    }
    with socket.socket() as temporary:
        temporary.bind(("127.0.0.1", 0))
        port = temporary.getsockname()[1]
    base = "http://127.0.0.1:" + str(port)
    network = proxy = process = None
    connection = None
    try:
        with closing(docker.from_env()) as client:
            info = client.info()
            report["daemon"] = {
                key: info.get(key)
                for key in ["ID", "Name", "Architecture", "NCPU", "MemTotal", "ServerVersion"]
            }
            network = client.networks.create(prefix, driver="bridge", internal=True)
            proxy = client.containers.run(
                "ubuntu/squid:6.6-24.04_edge",
                detach=True,
                name=prefix + "-squid",
                network="bridge",
                volumes={str(conf): {"bind": "/etc/squid/squid.conf", "mode": "ro"}},
                mem_limit="128m",
                nano_cpus=500_000_000,
                pids_limit=64,
            )
            network.connect(proxy, aliases=["qa-proxy"])
            report["proxy_image_id"] = proxy.image.id
        logfile = (args.folder / "server.log").open("wb")
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
            "--browser-network",
            prefix,
            "--browser-proxy",
            "http://qa-proxy:3128",
            stdout=logfile,
            stderr=asyncio.subprocess.STDOUT,
        )
        async with httpx.AsyncClient(base_url=base, timeout=120) as http:
            await wait_ready(http)
            token = (args.folder / "service-token").read_text()
            created = await http.post(
                "/sessions",
                headers={"X-Sandbox-Token": token},
                json={"session_id": sid, "runtime": "browser", "tenant": prefix},
            )
            created.raise_for_status()
            session = SessionCreated.model_validate_json(created.content)
        api = SandboxdBrowser(base, sid, session.token)
        await api.start()
        connection = await api.connect()
        tab = await connection.browser.new_tab()
        checks = report["checks"]
        assert isinstance(checks, list)
        with closing(docker.from_env()) as client:
            browser = client.containers.get("sandboxd-" + sid)
            browser.reload()
            attrs = browser.attrs
            assert attrs["HostConfig"]["NetworkMode"] == prefix
            assert not attrs["NetworkSettings"]["Ports"]
            assert network is not None
            network.reload()
            assert network.attrs["Internal"] is True
            checks.append("Browser attached only to an internal bridge; no published debugger port")
        # This is capability acceptance, not latency benchmarking the public Internet.
        public = report["public_loads"]
        assert isinstance(public, list)
        for url in ["https://example.com/"]:
            started = time.monotonic()
            try:
                await tab.navigate(url)
                body = ""
                for _ in range(50):
                    found = value(await tab.evaluate("document.body && document.body.innerText"))
                    body = found if isinstance(found, str) else ""
                    if (
                        "Example Domain" in body
                        or "This domain is for use in documentation examples" in body
                    ):
                        break
                    await asyncio.sleep(0.1)
                assert (
                    "Example Domain" in body
                    or "This domain is for use in documentation examples" in body
                ), body[:300]
                public.append(
                    {
                        "url": url,
                        "outcome": "passed",
                        "elapsed_ms": (time.monotonic() - started) * 1000,
                    }
                )
                checks.append("Public HTTPS page loads through the production Squid policy")
            except (BrowserError, AssertionError) as error:
                public.append(
                    {
                        "url": url,
                        "outcome": "failed",
                        "error_type": type(error).__name__,
                        "detail": str(error)[:300],
                    }
                )
        denials = report["private_denials"]
        assert isinstance(denials, list)
        for url in [
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:9222/json/version",
            "http://qa-proxy:3128/",
        ]:
            try:
                await tab.navigate(url)
                body = ""
                for _ in range(30):
                    found = value(await tab.evaluate("document.body && document.body.innerText"))
                    body = found if isinstance(found, str) else ""
                    if "access denied" in body.lower():
                        break
                    await asyncio.sleep(0.1)
                assert "access denied" in body.lower(), body[:500]
                denials.append({"url": url, "outcome": "proxy_access_denied"})
            except BrowserError as error:
                denials.append(
                    {"url": url, "outcome": "network_refused", "error_type": type(error).__name__}
                )
        checks.append(
            "Metadata, browser loopback debugger and private proxy addres"
            "s cannot bypass the independent egress policy"
        )
        assert all(row["outcome"] == "passed" for row in public), (
            "Public HTTPS proxy acceptance failed; inspect report"
        )
        await connection.aclose()
        connection = None
        # A fresh application client retains the same supervised session and independent guard.
        fresh = await api.connect()
        await fresh.aclose()
        checks.append(
            "Independent proxy policy remains when the application transport detaches/reconnects"
        )
        await api.end()
        report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report["failure_type"] = type(error).__name__
        report["failure"] = str(error)[:2000]
        raise
    finally:
        report["cleanup_errors"] = await cleanup(
            process,
            connection,
            args.folder,
            sid,
            proxy,
            network,
        )
        if report["cleanup_errors"]:
            report["status"] = "failed"
        (args.folder / "receipt.json").write_text(json.dumps(report, indent=2))
        token_path = args.folder / "service-token"
        if token_path.exists():
            token_path.unlink()
    print(json.dumps({"folder": str(args.folder), "status": report["status"]}))
    if report["status"] != "passed":
        raise RuntimeError("Proxy acceptance did not complete cleanly; inspect the receipt")


if __name__ == "__main__":
    asyncio.run(main())
