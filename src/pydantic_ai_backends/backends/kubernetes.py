"""A Kubernetes pod the workspaces and `sandboxd` run commands in.

Commands reach the pod through the API's `pods/exec` subresource, so the image
needs `/bin/sh` and nothing else of ours, and the caller needs `pods/exec` RBAC.
stdout and stderr arrive on their own channels and the exit status on a third;
the deadline is kept here, and a caller that times out or is cancelled stops the
command's process group with a second exec, as on Docker.

Not checked against a live cluster in this repository's CI: the unit tests drive
a fake API, and `tests/test_workspace_kubernetes_conformance.py` runs Pydantic
AI's conformance suite only when a cluster is reachable (`-m kubernetes`).
"""

from __future__ import annotations

import contextlib
import copy
import functools
import os
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import anyio
import anyio.to_thread

from pydantic_ai_backends._limits import MAX_RUN_OUTPUT_BYTES
from pydantic_ai_backends.backends._runner import (
    SIGNAL_EXIT_BASE,
    STOPPER,
    Sink,
    pid_file,
    wrapped_argv,
)
from pydantic_ai_backends.protocol import SandboxUnavailableError
from pydantic_ai_backends.types import CommandOutcome

DEFAULT_STARTUP_TIMEOUT = 60
"""Seconds to wait for a new pod to become Ready."""

DEFAULT_WORK_DIR = "/workspace"
"""Directory commands start in; an `emptyDir` in the default pod."""

STOP_GRACE_SECONDS = 5.0
"""Longest a stop may take, its TERM-then-KILL included, under cancellation."""

POLL_SECONDS = 0.2
"""How long one read of an exec's channels waits, which bounds how late a stop is seen."""

_CD_THEN_EXEC = 'cd "$1" && shift && exec "$@"'
"""`pods/exec` takes no working directory, so the command moves there itself."""


class KubernetesPodSandbox:
    """One pod: its lifecycle, and commands run in it under the workspace contract.

    Args:
        image: Container image. Needs `/bin/sh`.
        namespace: Namespace the pod lives in.
        sandbox_id: Identifier; the pod is named `pab-sandbox-<id>`, sanitised to
            DNS-1123. Generated when omitted.
        work_dir: Directory commands start in.
        pod_template: A full pod spec used instead of the default, with the
            name, image, labels and env filled in. It must keep its first
            container running, since commands are exec'd into it.
        kube_config_path: A kubeconfig; in-cluster config, then `~/.kube/config`
            when omitted.
        in_cluster: Force in-cluster or out-of-cluster auth.
        startup_timeout: Seconds to wait for `Ready` in :meth:`start`.
        idle_timeout: Idle seconds after which `SessionManager` may reap it.
        labels: Extra pod labels.
        env: Extra container environment.
        service_account_name: The pod's service account; a dedicated one with
            no permissions is the right choice.
    """

    def __init__(
        self,
        image: str,
        *,
        namespace: str = "default",
        sandbox_id: str | None = None,
        work_dir: str = DEFAULT_WORK_DIR,
        pod_template: dict[str, Any] | None = None,
        kube_config_path: str | None = None,
        in_cluster: bool | None = None,
        startup_timeout: int = DEFAULT_STARTUP_TIMEOUT,
        idle_timeout: int = 3_600,
        labels: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        service_account_name: str = "default",
    ) -> None:
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config

        self._image = image
        self._namespace = namespace
        self._id = sandbox_id or uuid.uuid4().hex
        self._pod_name = _sanitize_pod_name(self._id)
        self._work_dir = work_dir
        self._pod_template = pod_template
        self._startup_timeout = startup_timeout
        self._idle_timeout = idle_timeout
        self._labels = labels or {}
        self._env = env or {}
        self._service_account_name = service_account_name
        self._last_activity = time.time()

        if in_cluster is None:
            in_cluster = _detect_in_cluster()
        try:
            if in_cluster:
                k8s_config.load_incluster_config()
            elif kube_config_path:
                k8s_config.load_kube_config(config_file=kube_config_path)
            else:
                k8s_config.load_kube_config()
        except Exception as exc:
            raise RuntimeError(f"failed to load kubernetes config: {exc}") from exc
        self._core: Any = k8s_client.CoreV1Api()

    @property
    def id(self) -> str:
        """Unique identifier for this sandbox."""
        return self._id

    @property
    def pod_name(self) -> str:
        """The pod's name in its namespace."""
        return self._pod_name

    @property
    def work_dir(self) -> str:
        """Directory commands start in."""
        return self._work_dir

    @property
    def idle_timeout(self) -> int:
        """Idle seconds after which `SessionManager` may reap this sandbox."""
        return self._idle_timeout

    @property
    def last_activity(self) -> float:
        """Wall clock of the last operation, which idle cleanup reaps against."""
        return self._last_activity

    def touch(self) -> None:
        """Record activity, so idle cleanup does not reap a sandbox in use."""
        self._last_activity = time.time()

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Create the pod and wait until it is Ready.

        Raises:
            RuntimeError: The pod could not be created, finished before it was
                Ready, or was not Ready in `startup_timeout`; it is deleted again.
        """
        body = _build_pod_body(
            name=self._pod_name,
            image=self._image,
            namespace=self._namespace,
            work_dir=self._work_dir,
            extra_labels=self._labels,
            extra_env=self._env,
            service_account_name=self._service_account_name,
            override=self._pod_template,
        )
        try:
            self._core.create_namespaced_pod(self._namespace, body)
        except Exception as exc:
            raise RuntimeError(f"failed to create sandbox pod {self._pod_name}: {exc}") from exc
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            try:
                if self._ready():
                    return
            except SandboxUnavailableError as exc:
                self.stop()
                raise RuntimeError(f"sandbox pod {self._pod_name} did not start: {exc}") from exc
            time.sleep(0.5)
        self.stop()
        raise RuntimeError(f"sandbox pod {self._pod_name} not ready in {self._startup_timeout}s")

    def attach(self) -> None:
        """Check that the pod exists and is running, without creating anything.

        Raises:
            SandboxUnavailableError: There is no such pod, or it has finished.
        """
        if not self._ready():
            raise SandboxUnavailableError(f"pod {self._pod_name} is not running")

    def _ready(self) -> bool:
        """Whether the pod is Running and Ready.

        Raises:
            SandboxUnavailableError: The pod is gone or in a terminal phase.
        """
        from kubernetes.client.rest import ApiException

        try:
            pod: Any = self._core.read_namespaced_pod(self._pod_name, self._namespace)
        except ApiException as exc:
            if exc.status == 404:
                raise SandboxUnavailableError(f"pod {self._pod_name} does not exist") from exc
            raise
        phase = pod.status.phase
        if phase in ("Failed", "Succeeded"):
            raise SandboxUnavailableError(f"pod {self._pod_name} has finished ({phase})")
        conditions = pod.status.conditions or []
        return phase == "Running" and any(
            c.type == "Ready" and c.status == "True" for c in conditions
        )

    def is_alive(self) -> bool:
        """Whether the pod is running."""
        try:
            return self._ready()
        except Exception:
            return False

    def stop(self, purge: bool = False) -> None:
        """Delete the pod.

        Args:
            purge: Accepted for one signature across every sandbox; a pod keeps
                nothing a later attach could find, so stopping is deleting.
        """
        del purge
        with contextlib.suppress(Exception):
            self._core.delete_namespaced_pod(
                self._pod_name,
                self._namespace,
                grace_period_seconds=0,
                propagation_policy="Background",
            )

    # ── Commands ───────────────────────────────────────────────────────

    def _exec(self, argv: Sequence[str]) -> Any:
        from kubernetes.stream import stream

        return stream(
            self._core.connect_get_namespaced_pod_exec,
            self._pod_name,
            self._namespace,
            command=list(argv),
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False,
        )

    async def run_command(
        self,
        argv: Sequence[str],
        *,
        run_id: str,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        output_limit: int | None = None,
    ) -> CommandOutcome:
        """Run `argv` in the pod with stdin at EOF; see `CommandRunner.run_command`.

        Raises:
            SandboxUnavailableError: The pod is gone, before or during the command.
        """
        if not argv:
            raise ValueError("argv must name a program")
        self.touch()
        assignments = [f"{name}={value}" for name, value in (env or {}).items()]
        command = [
            "sh",
            "-c",
            _CD_THEN_EXEC,
            "sh",
            self._work_dir,
            "env",
            *assignments,
            *wrapped_argv(argv, run_id),
        ]
        sink = Sink(output_limit if output_limit is not None else MAX_RUN_OUTPUT_BYTES)
        stopped = threading.Event()
        try:
            response = await anyio.to_thread.run_sync(self._exec, command)
        except Exception as error:
            raise self._translate(error) from error

        finished = False
        exit_code: int | None = None
        try:
            with anyio.move_on_after(timeout) as deadline:
                await anyio.to_thread.run_sync(
                    functools.partial(_drain, response, sink, stopped), abandon_on_cancel=True
                )
            finished = not (deadline.cancelled_caught or sink.over_limit)
            if finished:
                # Read before the close below: the status arrives on its own channel.
                exit_code = response.returncode
            else:
                stdout, stderr = sink.text(partial=True)
                return CommandOutcome(
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=deadline.cancelled_caught,
                    output_limited=not deadline.cancelled_caught,
                )
        except Exception as error:
            raise self._translate(error) from error
        finally:
            if not finished:
                stopped.set()
                await self.stop_command(run_id)
            with contextlib.suppress(Exception):
                response.close()

        if exit_code is None or (
            exit_code > SIGNAL_EXIT_BASE and not await anyio.to_thread.run_sync(self.is_alive)
        ):
            raise SandboxUnavailableError(f"pod {self._pod_name} went away during the command")
        stdout, stderr = sink.text(partial=False)
        return CommandOutcome(stdout=stdout, stderr=stderr, exit_code=exit_code)

    async def stop_command(self, run_id: str) -> None:
        """Stop the command started under `run_id`, and everything it started.

        Best effort: the caller is already leaving with its own outcome.
        """

        def stop() -> None:
            response = self._exec(["sh", "-c", STOPPER, "sh", pid_file(run_id)])
            try:
                response.run_forever(timeout=STOP_GRACE_SECONDS)
            finally:
                response.close()

        with (
            anyio.CancelScope(shield=True),
            anyio.move_on_after(STOP_GRACE_SECONDS),
            contextlib.suppress(Exception),
        ):
            await anyio.to_thread.run_sync(stop, abandon_on_cancel=True)

    def _translate(self, error: BaseException) -> BaseException:
        """An API answer meaning the pod is gone, as that; anything else as is."""
        from kubernetes.client.rest import ApiException

        if isinstance(error, ApiException) and error.status in (404, 410):
            return SandboxUnavailableError(f"pod {self._pod_name} is gone: {error.reason}")
        return error


def _drain(response: Any, sink: Sink, stopped: threading.Event) -> None:
    """Copy an exec's channels into `sink` until it ends, overflows or is stopped."""
    while response.is_open() and not stopped.is_set():
        response.update(timeout=POLL_SECONDS)
        out = response.read_stdout() if response.peek_stdout() else None
        err = response.read_stderr() if response.peek_stderr() else None
        if not sink.add(
            out.encode("utf-8", errors="replace") if out else None,
            err.encode("utf-8", errors="replace") if err else None,
        ):
            return


def _detect_in_cluster() -> bool:
    return os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")


def _sanitize_pod_name(raw: str, *, prefix: str = "pab-sandbox-") -> str:
    """DNS-1123: lowercase, digits, hyphen; max 63 chars, must start+end alnum."""
    cleaned = "".join(c if (c.isalnum() or c == "-") else "-" for c in raw.lower())
    cleaned = cleaned.strip("-") or uuid.uuid4().hex
    return f"{prefix}{cleaned}"[:63].rstrip("-")


def _build_pod_body(
    *,
    name: str,
    image: str,
    namespace: str,
    work_dir: str,
    extra_labels: dict[str, str],
    extra_env: dict[str, str],
    service_account_name: str,
    override: dict[str, Any] | None,
) -> dict[str, Any]:
    """The pod to create: `override` with our parts filled in, or a hardened default."""
    if override is not None:
        # The caller's spec wins on everything except what a sandbox has to
        # control: its name, its image and the labels that find it.
        body: dict[str, Any] = copy.deepcopy(override)
        meta = body.setdefault("metadata", {})
        meta["name"] = name
        meta.setdefault("namespace", namespace)
        labels = meta.setdefault("labels", {})
        labels.setdefault("app", "pab-sandbox")
        for key, value in extra_labels.items():
            labels.setdefault(key, value)
        container = body.setdefault("spec", {}).setdefault("containers", [{}])[0]
        container["image"] = image
        container.setdefault("name", "sandbox")
        env_list: list[dict[str, str]] = list(container.get("env") or [])
        named = {entry.get("name") for entry in env_list}
        env_list.extend(
            {"name": key, "value": value} for key, value in extra_env.items() if key not in named
        )
        container["env"] = env_list
        return body

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": "pab-sandbox", **extra_labels},
        },
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "serviceAccountName": service_account_name,
            "terminationGracePeriodSeconds": 30,
            "enableServiceLinks": False,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "sandbox",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    # Commands are exec'd into it, so it only has to stay up.
                    "command": ["sleep", "infinity"],
                    "workingDir": work_dir,
                    "env": [
                        {"name": "PYTHONUNBUFFERED", "value": "1"},
                        *({"name": key, "value": value} for key, value in extra_env.items()),
                    ],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "runAsNonRoot": True,
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"memory": "256Mi", "cpu": "100m"},
                        "limits": {"memory": "768Mi", "cpu": "1"},
                    },
                    "volumeMounts": [
                        {"name": "workspace", "mountPath": work_dir},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }
            ],
            "volumes": [
                {"name": "workspace", "emptyDir": {"sizeLimit": "1Gi"}},
                {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
            ],
        },
    }
