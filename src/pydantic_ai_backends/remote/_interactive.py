"""Authenticated interactive attachments to existing sandboxd sessions.

CDP is a privileged application integration, like /run, never a browser UI API.
Its destination is fixed to the authorized container's loopback; neither URLs
nor ports come from a caller. The managed container survives client disconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from shlex import quote
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, runtime_checkable
from uuid import uuid4

import anyio
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from pydantic_ai_backends.backends.docker._browser_bridge import (
    BrowserChannel,
    BrowserChannelError,
)
from pydantic_ai_backends.backends.docker.session import alive_of
from pydantic_ai_backends.protocol import CommandRunner, SandboxProbeError, SandboxUnavailableError
from pydantic_ai_backends.remote import _browser_watchdog
from pydantic_ai_backends.remote._environments import (
    EnvironmentConflictError,
    EnvironmentLease,
    EnvironmentSnapshot,
    EnvironmentUnavailableError,
    ManagedEnvironmentRegistry,
)
from pydantic_ai_backends.remote.wire import TOKEN_HEADER

if TYPE_CHECKING:
    from pydantic_ai_backends.remote.server import _Service

logger = logging.getLogger(__name__)
Kind = Literal["browser", "desktop"]
TokenHeader = Annotated[str | None, Header(alias=TOKEN_HEADER)]
_OBJECT = TypeAdapter(dict[str, object])

_WATCHDOG_SOURCE = Path(_browser_watchdog.__file__).read_text()

# Installed by the operator's explicit browser runtime, not on demand by a viewer.
_BROWSER_START = r"""
set -eu
watchdog() { python3 -c __WATCHDOG_SOURCE__ "$@"; }
browser_pid=0
display_pid=0
trap 'watchdog abort "$browser_pid" "$display_pid"' EXIT
for tool in chromium Xvfb python3; do command -v "$tool" >/dev/null; done
python3 -c 'import websocket'
test -n "${SANDBOXD_BROWSER_PROXY:-}" || exit 4
probe="import urllib.request; urllib.request.urlopen('http://127.0.0.1:9222/json/version',timeout=1)"
if python3 -c "$probe" 2>/dev/null; then
    watchdog existing
    trap - EXIT
    exit 0
fi
DISPLAY=:98
export DISPLAY
test ! -S /tmp/.X11-unix/X98 || exit 5
setsid nohup Xvfb :98 -screen 0 __WIDTH__x__HEIGHT__x24 -nolisten tcp -ac >/dev/null 2>&1 &
display_pid=$!
# The container is the process-isolation boundary; no host debugger port exists.
setsid nohup chromium --no-sandbox --no-first-run --no-default-browser-check \
    --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
    --window-size=__WIDTH__,__HEIGHT__ \
    --user-data-dir=/tmp/sandboxd-browser-profile --password-store=basic \
    --proxy-server="$SANDBOXD_BROWSER_PROXY" --proxy-bypass-list="<-loopback>" \
    --disable-quic --force-webrtc-ip-handling-policy=disable_non_proxied_udp \
    --disable-dev-shm-usage about:blank >/tmp/sandboxd-browser.log 2>&1 &
browser_pid=$!
for _ in $(seq 100); do
    if python3 -c "$probe" 2>/dev/null; then
        watchdog arm __LIFETIME__ "$browser_pid" "$display_pid"
        trap - EXIT
        exit 0
    fi
    sleep 0.1
done
exit 1
"""


class EnvironmentStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idle_seconds: int = Field(default=900, ge=1, le=86400)
    lifetime_seconds: int = Field(default=3600, ge=1, le=86400)
    width: int = Field(default=1280, ge=640, le=1920)
    height: int = Field(default=800, ge=480, le=1200)


class LeaseToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=256, repr=False)


class LeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(min_length=1, max_length=128)


@runtime_checkable
class ActiveSandbox(Protocol):
    def touch(self) -> None: ...


@runtime_checkable
class FreshSandbox(Protocol):
    def is_alive_fresh(self) -> bool: ...


@runtime_checkable
class BrowserProvider(Protocol):
    async def open_browser_channel(self) -> BrowserChannel: ...


@dataclass
class InteractiveResource:
    service: _Service
    session_id: str
    sandbox: object
    kind: Kind

    async def aclose(self) -> None:
        # Ending Browser's dedicated runtime stops all tabs, while workspace files
        # remain subject to storage retention. Desktop can share a code workspace.
        if self.kind == "browser":
            try:
                current = self.service.peek(self.session_id)
            except HTTPException as error:
                if error.status_code == 404:
                    return
                raise
            if current is self.sandbox:
                await self.service.close_session(self.session_id)


def _key(session_id: str, kind: Kind) -> str:
    return f"{session_id}:{kind}"


CDP_COMMAND_SECONDS = 30.0
FRESH_PROBE_SECONDS = 10.0
MAX_PENDING_COMMANDS = 128


class _BrowserRelay:
    """Correlate CDP replies while allowing request interception to make progress."""

    def __init__(
        self, gateway: InteractiveGateway, session_id: str, channel: BrowserChannel
    ) -> None:
        self.gateway = gateway
        self.session_id = session_id
        self.channel = channel
        self.pending: dict[int, asyncio.Future[None]] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.active: dict[int, asyncio.Task[None]] = {}
        self.failure: asyncio.Future[BaseException] = asyncio.get_running_loop().create_future()
        self.owner = f"cdp:{uuid4().hex}"

    async def failed(self) -> None:
        raise await self.failure

    def _completed(self, identifier: int, task: asyncio.Task[None]) -> None:
        self.tasks.discard(task)
        self.active.pop(identifier, None)
        if not task.cancelled():
            error = task.exception()
            if error is not None and not self.failure.done():
                self.failure.set_result(error)

    async def acknowledge(self, payload: dict[str, object]) -> None:
        identifier = payload.get("id")
        if isinstance(identifier, int) and not isinstance(identifier, bool):
            future = self.pending.pop(identifier, None)
            if future is not None:
                future.set_result(None)
                active = self.active.get(identifier)
                if active is not None:
                    # A caller may send its next input immediately on receipt.
                    # Publish the acknowledgement only after lease cleanup finishes.
                    await active

    async def forward(
        self,
        payload: dict[str, object],
        *,
        active: bool,
        sandbox: object,
        control: str | None,
    ) -> None:
        identifier = payload.get("id")
        if not isinstance(identifier, int) or isinstance(identifier, bool):
            raise BrowserChannelError("Browser commands require an integer identity")
        if (
            identifier in self.pending
            or identifier in self.active
            or len(self.pending) >= MAX_PENDING_COMMANDS
        ):
            raise BrowserChannelError("Browser command identity repeated or pending limit reached")
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        message = json.dumps(payload)
        if active:
            task = asyncio.create_task(self._active(identifier, future, message, sandbox, control))
            self.tasks.add(task)
            self.active[identifier] = task
            task.add_done_callback(partial(self._completed, identifier))
        else:
            # Fetch.continueRequest/auth replies must flow while Page.navigate
            # owns the physical lease and is waiting for those replies itself.
            await self.channel.send(message)

    async def _active(
        self,
        identifier: int,
        future: asyncio.Future[None],
        message: str,
        sandbox: object,
        control: str | None,
    ) -> None:
        registry = self.gateway.registry
        key = _key(self.session_id, "browser")
        lease = registry.acquire(key, self.owner) if control is None else None
        try:
            async with registry.use(
                key, token=control if lease is None else lease.token, activity=True
            ):
                if isinstance(sandbox, ActiveSandbox):
                    sandbox.touch()
                try:
                    await self.channel.send(message)
                    await asyncio.wait_for(asyncio.shield(future), CDP_COMMAND_SECONDS)
                except TimeoutError as error:
                    # The command may already be running. Block further input
                    # until bounded recovery/retirement instead of replaying it.
                    raise BrowserChannelError(
                        "Browser command timed out; outcome unknown"
                    ) from error
                finally:
                    if not future.done():
                        with suppress(EnvironmentUnavailableError):
                            registry.mark_unavailable(key)
        finally:
            if self.pending.get(identifier) is future:
                del self.pending[identifier]
            if not future.done():
                future.cancel()
            if lease is not None:
                with suppress(EnvironmentConflictError, EnvironmentUnavailableError):
                    registry.release(key, lease.token)

    async def aclose(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for future in self.pending.values():
            future.cancel()
        self.pending.clear()
        self.active.clear()
        if not self.failure.done():
            self.failure.cancel()


class InteractiveGateway:
    def __init__(self, service: _Service) -> None:
        self.service = service
        self.registry: ManagedEnvironmentRegistry[InteractiveResource] = ManagedEnvironmentRegistry(
            max_environments=service.config.max_interactive_environments
        )

    def authorize(self, session_id: str, token: str | None) -> None:
        self.service.check_session_token(session_id, token)

    async def _original(
        self, session_id: str, kind: Kind, *, fresh: bool = True
    ) -> InteractiveResource:
        """Require the admitted live container; never wake or silently replace it."""
        key = _key(session_id, kind)
        snapshot = self.registry.inspect(key)
        if snapshot.state == "unavailable":
            raise HTTPException(503, "The original environment is temporarily unavailable")
        resource = self.registry.resource(key)
        try:
            current: object = self.service.peek(session_id)
        except HTTPException as error:
            if error.status_code != 404:
                raise
            self.registry.mark_gone(key)
            raise EnvironmentUnavailableError("The original environment is gone") from error
        alive = False
        if current is resource.sandbox:
            try:
                if fresh and isinstance(current, FreshSandbox):
                    with anyio.fail_after(FRESH_PROBE_SECONDS):
                        alive = await anyio.to_thread.run_sync(
                            current.is_alive_fresh, abandon_on_cancel=True
                        )
                else:
                    alive = await alive_of(current)
            except (SandboxProbeError, TimeoutError) as error:
                raise HTTPException(503, "The original environment could not be checked") from error
        if not alive:
            self.registry.mark_gone(key)
            raise EnvironmentUnavailableError("The original environment container is gone")
        return resource

    async def open_resource(
        self, session_id: str, kind: Kind, body: EnvironmentStart
    ) -> InteractiveResource:
        # Explicit Start may wake an existing workspace. Reads and CDP attach
        # never call sandbox(), whose healing behavior would hide lost DOM.
        sandbox: object = await self.service.sandbox(session_id)
        if not isinstance(sandbox, CommandRunner):
            raise HTTPException(501, "This sandbox cannot open interactive environments")
        runtime = self.service.describe(session_id, sandbox, alive=True).runtime
        if kind == "browser" and runtime != "browser":
            raise HTTPException(409, "Browser needs its dedicated browser runtime")
        if kind == "browser":
            script = (
                _BROWSER_START.replace("__WIDTH__", str(body.width))
                .replace("__HEIGHT__", str(body.height))
                .replace(
                    "__LIFETIME__",
                    str(min(body.lifetime_seconds, self.service.config.interactive_max_lifetime)),
                )
                .replace("__WATCHDOG_SOURCE__", quote(_WATCHDOG_SOURCE))
            )
        else:
            from pydantic_ai_backends.desktop import desktop_start

            script = desktop_start(body.width, body.height)
        try:
            with self.service.observe(session_id, "environment-start", "desktop"):
                result = await sandbox.run_command(
                    ["/bin/sh", "-c", script],
                    run_id=uuid4().hex,
                    timeout=90,
                    output_limit=4096,
                )
            if result.exit_code != 0:
                raise HTTPException(503, "The configured runtime could not start its environment")
        except BaseException:
            if kind == "browser":
                with anyio.move_on_after(10, shield=True) as cleanup:
                    try:
                        await self.service.close_session(session_id)
                    except Exception as error:
                        logger.warning(
                            "Browser provision cleanup failed: %s (%s)",
                            session_id,
                            type(error).__name__,
                        )
                if cleanup.cancel_called:
                    logger.warning("Browser provision cleanup timed out: %s", session_id)
            raise
        return InteractiveResource(self.service, session_id, sandbox, kind)

    async def start(
        self, session_id: str, kind: Kind, body: EnvironmentStart, token: TokenHeader = None
    ) -> EnvironmentSnapshot:
        self.authorize(session_id, token)

        try:
            try:
                existing = self.registry.inspect(_key(session_id, kind))
            except EnvironmentUnavailableError:
                existing = None
            if existing is not None and existing.state in ("ready", "unavailable"):
                await self._original(session_id, kind)
            snapshot = await self.registry.ensure(
                _key(session_id, kind),
                kind=kind,
                open_resource=lambda: self.open_resource(session_id, kind, body),
                idle_seconds=min(
                    body.idle_seconds,
                    self.service.config.interactive_idle_timeout,
                    self.service.config.idle_timeout
                    if self.service.config.idle_timeout > 0
                    else self.service.config.interactive_idle_timeout,
                ),
                lifetime_seconds=min(
                    body.lifetime_seconds, self.service.config.interactive_max_lifetime
                ),
            )
        except EnvironmentConflictError as error:
            raise HTTPException(409, str(error)) from error
        except EnvironmentUnavailableError as error:
            raise HTTPException(410, str(error)) from error
        return snapshot

    async def inspect(
        self, session_id: str, kind: Kind, token: TokenHeader = None
    ) -> EnvironmentSnapshot:
        self.authorize(session_id, token)
        try:
            await self._original(session_id, kind)
            return self.registry.inspect(_key(session_id, kind))
        except EnvironmentUnavailableError as error:
            raise HTTPException(410, str(error)) from error

    async def end(self, session_id: str, kind: Kind, token: TokenHeader = None) -> None:
        self.authorize(session_id, token)
        try:
            self.registry.inspect(_key(session_id, kind))
            registered = True
        except EnvironmentUnavailableError:
            registered = False
        await self.registry.close(_key(session_id, kind))
        if kind == "browser" and not registered:
            # A client may have journaled the workspace before Start was admitted.
            # End must settle that known dedicated container even without a GUI row.
            try:
                sandbox: object = self.service.peek(session_id)
            except HTTPException as error:
                if error.status_code == 404:
                    return
                raise
            if self.service.describe(session_id, sandbox, alive=True).runtime == "browser":
                await self.service.close_session(session_id)

    async def lease(
        self, session_id: str, kind: Kind, body: LeaseRequest, token: TokenHeader = None
    ) -> EnvironmentLease:
        self.authorize(session_id, token)
        try:
            await self._original(session_id, kind)
            return self.registry.acquire(_key(session_id, kind), body.owner)
        except EnvironmentConflictError as error:
            raise HTTPException(409, str(error)) from error
        except EnvironmentUnavailableError as error:
            raise HTTPException(410, str(error)) from error

    async def renew_lease(
        self, session_id: str, kind: Kind, body: LeaseToken, token: TokenHeader = None
    ) -> EnvironmentLease:
        self.authorize(session_id, token)
        try:
            await self._original(session_id, kind)
            return self.registry.renew(_key(session_id, kind), body.token)
        except EnvironmentConflictError as error:
            raise HTTPException(409, str(error)) from error
        except EnvironmentUnavailableError as error:
            raise HTTPException(410, str(error)) from error

    async def release_lease(
        self, session_id: str, kind: Kind, body: LeaseToken, token: TokenHeader = None
    ) -> None:
        self.authorize(session_id, token)
        try:
            await self._original(session_id, kind)
            self.registry.release(_key(session_id, kind), body.token)
        except EnvironmentConflictError as error:
            raise HTTPException(409, str(error)) from error
        except EnvironmentUnavailableError as error:
            raise HTTPException(410, str(error)) from error

    async def _detach(self, channel: BrowserChannel) -> None:
        """A failed socket teardown must not disguise the request's actual outcome."""
        with anyio.move_on_after(5, shield=True) as cleanup:
            try:
                await channel.aclose()
            except Exception as error:
                logger.warning("Browser channel detach failed (%s)", type(error).__name__)
        if cleanup.cancel_called:
            logger.warning("Browser channel detach timed out")

    async def browser_socket(self, websocket: WebSocket, session_id: str) -> None:
        try:
            self.authorize(session_id, websocket.headers.get(TOKEN_HEADER))
            resource = await self._original(session_id, "browser")
            sandbox = resource.sandbox
            if not isinstance(sandbox, BrowserProvider):
                raise HTTPException(501, "The runtime does not support private browser channels")
        except HTTPException as error:
            await websocket.close(code=1013 if error.status_code == 503 else 1008)
            return
        except EnvironmentUnavailableError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        channel: BrowserChannel | None = None
        relay: _BrowserRelay | None = None
        try:
            channel = await sandbox.open_browser_channel()
            relay = _BrowserRelay(self, session_id, channel)

            tasks = [
                asyncio.create_task(self.incoming(websocket, session_id, sandbox, relay)),
                asyncio.create_task(self.outgoing(websocket, session_id, sandbox, relay)),
                asyncio.create_task(relay.failed()),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                # Close the socket first, unblocking its thread before joining.
                await self._detach(channel)
                await asyncio.gather(*tasks, return_exceptions=True)
                await relay.aclose()
        except (
            WebSocketDisconnect,
            BrowserChannelError,
            EnvironmentUnavailableError,
            EnvironmentConflictError,
            ValidationError,
            HTTPException,
            SandboxUnavailableError,
        ):
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=1011)
        finally:
            if channel is not None:
                await self._detach(channel)

    async def incoming(
        self, websocket: WebSocket, session_id: str, sandbox: object, relay: _BrowserRelay
    ) -> None:
        while True:
            # Trusted CDP clients have the same authority as /run. No UI
            # receives this token; product-level input gates remain upstream.
            message = await websocket.receive_text()
            self.authorize(session_id, websocket.headers.get(TOKEN_HEADER))
            resource = await self._original(session_id, "browser", fresh=False)
            if resource.sandbox is not sandbox:
                raise EnvironmentUnavailableError(
                    "The browser channel belongs to an older container"
                )
            parsed = _OBJECT.validate_json(message)
            if parsed.get("method") == "Target.createBrowserContext":
                params = parsed.get("params", {})
                if not isinstance(params, dict):
                    raise BrowserChannelError("Invalid context parameters")
                parsed["params"] = {**params, "disposeOnDetach": False}
                message = json.dumps(parsed)
            method = parsed.get("method")
            active = isinstance(method, str) and (
                method.startswith("Input.")
                or method
                in (
                    "Page.navigate",
                    "Page.reload",
                    "Page.navigateToHistoryEntry",
                    "Target.createTarget",
                )
            )
            await relay.forward(
                parsed,
                active=active,
                sandbox=sandbox,
                control=websocket.headers.get("X-Environment-Lease"),
            )

    async def outgoing(
        self, websocket: WebSocket, session_id: str, sandbox: object, relay: _BrowserRelay
    ) -> None:
        while True:
            message = await relay.channel.receive()
            self.authorize(session_id, websocket.headers.get(TOKEN_HEADER))
            resource = await self._original(session_id, "browser", fresh=False)
            if resource.sandbox is not sandbox:
                raise EnvironmentUnavailableError(
                    "The browser channel belongs to an older container"
                )
            await relay.acknowledge(_OBJECT.validate_json(message))
            await websocket.send_text(message)


def register_interactive_routes(
    app: FastAPI, service: _Service
) -> ManagedEnvironmentRegistry[InteractiveResource]:
    gateway = InteractiveGateway(service)
    path = "/sessions/{session_id}/environments/{kind}"
    app.add_api_route(path, gateway.start, methods=["POST"])
    app.add_api_route(path, gateway.inspect, methods=["GET"])
    app.add_api_route(path, gateway.end, methods=["DELETE"], status_code=204)
    app.add_api_route(path + "/lease", gateway.lease, methods=["POST"])
    app.add_api_route(path + "/lease/renew", gateway.renew_lease, methods=["POST"])
    app.add_api_route(
        path + "/lease/release", gateway.release_lease, methods=["POST"], status_code=204
    )
    app.add_api_websocket_route("/sessions/{session_id}/browser/cdp", gateway.browser_socket)
    return gateway.registry


async def maintain_interactive(registry: ManagedEnvironmentRegistry[InteractiveResource]) -> None:
    """Retire by deadline without screenshot work or client traffic."""
    while True:
        await asyncio.sleep(1)
        for failed in await registry.sweep():
            logger.warning("Interactive cleanup failed: %s", failed.environment_id)
