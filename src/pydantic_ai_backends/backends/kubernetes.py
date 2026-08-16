"""KubernetesPodSandbox — runs the agent's shell tools inside a K8s pod.

Implements the `BaseSandbox` ABC with **synchronous** methods
(matching `DockerSandbox` and `DaytonaSandbox`) and is intended to be
a drop-in for any user of `pydantic_ai_backends.SessionManager`.

Two ways to use it:

1. **In-cluster, with an in-pod HTTP exec server** (recommended for
   long-running tool calls — `npm install`, headless browser, MCP
   servers). This is the `mode="http"` path. The pod image must
   expose an HTTP endpoint at `/exec`, `/read`, etc. (see
   ``examples/kubernetes-sandbox.md`` for a reference image).
2. **Anywhere, using the K8s API ``pods/exec`` subresource**. This is
   the ``mode="api"`` path. Fine for short commands; can stall on
   long output streams. Needs ``pods/exec`` RBAC on the caller.

Either way, ``KubernetesPodSandbox.start()`` waits for the pod to be
``Ready`` and returns; ``stop()`` deletes the pod.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import os
import secrets
import time
import uuid
from typing import Any, Literal

from pydantic_ai_backends._editing import Replacement, replace_in_content
from pydantic_ai_backends._limits import MAX_EXECUTE_OUTPUT_BYTES
from pydantic_ai_backends.backends.base import BaseSandbox
from pydantic_ai_backends.types import EditResult, ExecuteResponse, FileInfo, WriteResult

DEFAULT_EXEC_TIMEOUT = 30 * 60  # Matches DaytonaSandbox.
DEFAULT_STARTUP_TIMEOUT = 60
DEFAULT_PORT = 8080


class KubernetesPodSandbox(BaseSandbox):
    """Sandbox backed by a Kubernetes pod.

    Args:
        image: container image to run.
        namespace: K8s namespace to create the pod in.
        sandbox_id: identifier; used as a deterministic pod name suffix,
            sanitized to DNS-1123. Auto-generated if not provided.
        session_id: alias for ``sandbox_id`` (accepted for parity with
            ``DockerSandbox``). Ignored if ``sandbox_id`` is set.
        mode: ``"http"`` (default) talks to an in-pod exec server on
            ``port``; ``"api"`` uses the K8s ``pods/exec`` subresource.
            ``mode="api"`` shells out as ``timeout <n> sh -c <command>``,
            so the image must provide ``/bin/sh`` **and** a ``timeout``
            binary (GNU coreutils or the BusyBox applet). Stock images
            such as ``python:*``, ``node:*``, ``debian``, ``ubuntu`` and
            ``alpine`` all ship it; minimal/``distroless`` images without
            a shell do not and will surface ``timeout: not found``.
        port: port the in-pod exec server listens on (mode="http" only).
        exec_token: shared secret sent in the ``X-Sandbox-Token`` header
            (mode="http" only). Auto-generated if not provided; written
            into the pod env as ``SANDBOX_EXEC_TOKEN``.
        pod_template: optional dict — full pod spec body. If set, used
            verbatim (with ``image`` / env / labels filled in). Otherwise
            a sensible default is built (single container, non-root,
            readOnlyRootFilesystem, 768Mi/1CPU limit).
        kube_config_path: path to a kubeconfig. If unset, falls back to
            in-cluster config, then ``~/.kube/config``.
        in_cluster: force in-cluster vs out-of-cluster auth.
        startup_timeout: seconds to wait for ``Ready`` in ``start()``.
        idle_timeout: passed through to ``SessionManager`` for parity.
        labels: extra labels merged into the pod metadata.
        env: extra environment variables for the container.
        delete_on_stop: whether ``stop()`` deletes the pod. Default True.
        service_account_name: ``serviceAccountName`` for the pod
            (defaults to ``default``; recommend a dedicated, no-privilege SA).

    Lifecycle:
        - ``__init__`` validates args and builds the in-memory client. It
          does **not** create the pod — call ``start()`` (or let
          ``SessionManager.get_or_create()`` call it) to actually create.
        - ``start()`` creates the pod and blocks until Ready.
        - ``execute()``, ``edit()``, ``read()``, ``write()``, ``glob_info``,
          ``grep_raw``, ``ls_info``: synchronous, talk to the pod (HTTP
          or pods/exec) and return.
        - ``stop()`` deletes the pod (if ``delete_on_stop=True``);
          idempotent; never raises.

    Errors:
        - Constructor raises ``ImportError`` if the ``kubernetes`` SDK
          is not installed (``pip install pydantic-ai-backend[kubernetes]``).
        - ``start()`` raises ``RuntimeError`` if the pod doesn't reach
          ``Ready`` in ``startup_timeout``.
        - ``execute()`` never raises; runtime errors are surfaced as
          ``ExecuteResponse(output="Error: ...", exit_code=1)``.
    """

    def __init__(
        self,
        image: str,
        *,
        namespace: str = "default",
        sandbox_id: str | None = None,
        session_id: str | None = None,  # alias, parity with DockerSandbox
        mode: Literal["http", "api"] = "http",
        port: int = DEFAULT_PORT,
        exec_token: str | None = None,
        pod_template: dict[str, Any] | None = None,
        kube_config_path: str | None = None,
        in_cluster: bool | None = None,
        startup_timeout: int = DEFAULT_STARTUP_TIMEOUT,
        idle_timeout: int = 3_600,
        labels: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        delete_on_stop: bool = True,
        service_account_name: str = "default",
    ) -> None:
        try:
            from kubernetes import client as k8s_client
            from kubernetes import config as k8s_config
            from kubernetes.client import ApiClient as _ApiClient  # noqa: F401
            from kubernetes.client.rest import ApiException as _ApiException  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "kubernetes SDK is required. Install with: "
                "pip install pydantic-ai-backend[kubernetes]"
            ) from exc

        self._k8s_client = k8s_client
        self._k8s_config = k8s_config

        if mode == "http":
            try:
                import httpx as _httpx
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "httpx is required for mode='http'. Install with: "
                    "pip install pydantic-ai-backend[kubernetes]"
                ) from exc
            self._httpx = _httpx

        self._image = image
        self._namespace = namespace
        self._mode = mode
        self._port = port
        self._exec_token = exec_token or secrets.token_urlsafe(32)
        self._pod_template = pod_template
        self._startup_timeout = startup_timeout
        self._idle_timeout = idle_timeout
        self._extra_labels = labels or {}
        self._extra_env = env or {}
        self._delete_on_stop = delete_on_stop
        self._service_account_name = service_account_name

        # Identity. Pod names must be DNS-1123 compliant.
        provided_id = sandbox_id or session_id
        if provided_id is not None:
            super().__init__(provided_id)
        else:
            super().__init__(uuid.uuid4().hex)
        self._pod_name = _sanitize_pod_name(self._id, prefix="pab-sandbox-")

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

        self._core = k8s_client.CoreV1Api()
        self._pod_ip: str | None = None
        self._http: Any = None
        self._stopped = False

    def start(self) -> None:
        """Create the pod and wait for it to be Ready."""
        body = _build_pod_body(
            name=self._pod_name,
            image=self._image,
            namespace=self._namespace,
            port=self._port,
            mode=self._mode,
            exec_token=self._exec_token,
            extra_labels=self._extra_labels,
            extra_env=self._extra_env,
            service_account_name=self._service_account_name,
            override=self._pod_template,
        )
        try:
            self._core.create_namespaced_pod(self._namespace, body)
        except Exception as exc:
            self._cleanup_quietly()
            raise RuntimeError(f"failed to create sandbox pod {self._pod_name}: {exc}") from exc

        deadline = time.time() + self._startup_timeout
        while time.time() < deadline:
            pod: Any = self._core.read_namespaced_pod(self._pod_name, self._namespace)
            phase = pod.status.phase
            if phase in ("Failed", "Succeeded"):
                self._cleanup_quietly()
                raise RuntimeError(f"sandbox pod {self._pod_name} entered terminal phase {phase}")
            if phase == "Running" and pod.status.pod_ip:
                conditions = pod.status.conditions or []
                if any(c.type == "Ready" and c.status == "True" for c in conditions):
                    self._pod_ip = pod.status.pod_ip
                    if self._mode == "http":
                        self._http = self._httpx.Client(
                            base_url=f"http://{self._pod_ip}:{self._port}",
                            timeout=self._httpx.Timeout(
                                connect=2.0,
                                read=DEFAULT_EXEC_TIMEOUT,
                                write=10.0,
                                pool=2.0,
                            ),
                            headers={"X-Sandbox-Token": self._exec_token},
                        )
                    return
            time.sleep(0.5)

        self._cleanup_quietly()
        raise RuntimeError(f"sandbox pod {self._pod_name} not ready in {self._startup_timeout}s")

    def is_alive(self) -> bool:
        if self._stopped:
            return False
        try:
            pod: Any = self._core.read_namespaced_pod(self._pod_name, self._namespace)
        except Exception:
            return False
        return bool(pod.status.phase == "Running")

    def stop(self, purge: bool = False) -> None:
        """Close the connection and delete the pod when this sandbox owns it.

        Args:
            purge: Accepted for one signature across every sandbox, and it makes
                no difference here. A pod is deleted or it is not, and that is
                `delete_on_stop`'s decision rather than the caller's - there is
                no state kept for a later attach to preserve.
        """
        if self._stopped:
            return
        self._stopped = True
        if self._http is not None:
            with contextlib.suppress(Exception):
                self._http.close()
        if self._delete_on_stop:
            self._cleanup_quietly()

    def __del__(self) -> None:  # pragma: no cover
        with contextlib.suppress(Exception):
            self.stop()

    def _cleanup_quietly(self) -> None:
        with contextlib.suppress(Exception):
            self._core.delete_namespaced_pod(
                self._pod_name,
                self._namespace,
                grace_period_seconds=30,
                propagation_policy="Background",
            )

    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        self.touch()
        timeout_seconds = timeout if timeout is not None else DEFAULT_EXEC_TIMEOUT

        if self._mode == "http":
            return self._execute_http(command, timeout_seconds)
        return self._execute_api(command, timeout_seconds)

    def edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """Edit a file by fetching it, replacing in Python and writing it back."""
        self.touch()
        content = self.read_bytes(path).decode("utf-8", errors="replace")
        if not content:
            return EditResult(error=f"File '{path}' not found")

        outcome = replace_in_content(content, old_string, new_string, replace_all)
        if not isinstance(outcome, Replacement):
            return EditResult(error=outcome)

        written = self.write(path, outcome.content)
        if written.error is not None:
            return EditResult(error=written.error)
        return EditResult(path=path, occurrences=outcome.occurrences)

    def _execute_http(self, command: str, timeout_seconds: int) -> ExecuteResponse:
        try:
            r = self._http.post(
                "/exec",
                json={"command": command, "timeout_seconds": timeout_seconds},
                timeout=timeout_seconds + 5,
            )
            r.raise_for_status()
        except Exception as exc:
            return ExecuteResponse(output=f"Error: {exc}", exit_code=1, truncated=False)

        body = r.json()
        return ExecuteResponse(
            output=body.get("output", "")[:MAX_EXECUTE_OUTPUT_BYTES],
            exit_code=body.get("exit_code"),
            truncated=bool(body.get("truncated")),
        )

    def _execute_api(self, command: str, timeout_seconds: int) -> ExecuteResponse:
        from kubernetes.stream import stream  # WPS433: lazy

        # Wrap with timeout so a hung command can't hang us forever.
        # Requires `timeout` (coreutils / BusyBox) and `/bin/sh` in the
        # image; absent on distroless. See the class docstring (`mode`).
        wrapped = ["timeout", str(timeout_seconds), "sh", "-c", command]
        try:
            resp = stream(
                self._core.connect_get_namespaced_pod_exec,
                self._pod_name,
                self._namespace,
                command=wrapped,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
        except Exception as exc:
            return ExecuteResponse(output=f"Error: {exc}", exit_code=1, truncated=False)

        # `close` in `finally`: the read loop below can raise on a dropped
        # connection, and the websocket would then stay open for the life of the
        # process — one leaked socket per failed command.
        try:
            output = bytearray()
            while resp.is_open() and len(output) < MAX_EXECUTE_OUTPUT_BYTES:
                resp.update(timeout=1)
                if resp.peek_stdout():
                    output.extend(resp.read_stdout().encode("utf-8", errors="replace"))
                if resp.peek_stderr():
                    output.extend(resp.read_stderr().encode("utf-8", errors="replace"))
            # `1` for an unknown status, never `0`. The loop also exits once the
            # output cap is hit, with the command still running and no return
            # code yet — reporting that as success told the caller a truncated
            # command had passed. `LocalBackend` already defaults to `1`.
            exit_code = resp.returncode if resp.returncode is not None else 1
        except Exception as exc:
            return ExecuteResponse(output=f"Error: {exc}", exit_code=1, truncated=False)
        finally:
            with contextlib.suppress(Exception):
                resp.close()

        truncated = len(output) >= MAX_EXECUTE_OUTPUT_BYTES
        return ExecuteResponse(
            output=bytes(output[:MAX_EXECUTE_OUTPUT_BYTES]).decode("utf-8", errors="replace"),
            exit_code=exit_code,
            truncated=truncated,
        )

    def read_bytes(self, path: str) -> bytes:
        # Match LocalBackend semantics: return b"" on missing / transport /
        # validation errors. The console toolset's context-file probe walks
        # well-known paths (/AGENTS.md, /SOUL.md, ...) on every turn; if we
        # encoded the error as bytes here, the probe would treat the error
        # string as legit file content and inject it into the system prompt.
        self.touch()
        if self._mode == "http":
            try:
                r = self._http.post("/read", json={"path": path, "offset": 0, "limit": 10**9})
            except Exception:
                return b""
            if r.status_code >= 400:
                return b""
            content: str = r.json().get("content", "") or ""
            return content.encode("utf-8")
        return super().read_bytes(path)

    def read(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        # Match LocalBackend: return "Error: ..." strings rather than raise.
        # The upstream read_file tool wrapper (str_replace edit_format) does
        # not catch exceptions; anything raised here propagates through the
        # capability layer and kills the agent run.
        self.touch()
        if self._mode == "http":
            try:
                r = self._http.post("/read", json={"path": path, "offset": offset, "limit": limit})
            except Exception as exc:
                return f"Error: {exc}"
            if r.status_code == 404:
                return f"Error: File '{path}' not found"
            if r.status_code >= 400:
                return f"Error: HTTP {r.status_code}"
            text: str = r.json().get("content", "") or ""
            return text
        return super().read(path, offset, limit)

    def ls_info(self, path: str) -> list[FileInfo]:
        # LocalBackend.ls_info returns [] on missing / out-of-workspace; mirror
        # so the run survives a model probing a bogus path.
        self.touch()
        if self._mode == "http":
            try:
                r = self._http.post("/ls", json={"path": path})
            except Exception:
                return []
            if r.status_code >= 400:
                return []
            return [_to_file_info(row) for row in r.json()]
        return super().ls_info(path)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        self.touch()
        if self._mode == "http":
            try:
                r = self._http.post("/glob", json={"pattern": pattern, "path": path})
            except Exception:
                return []
            if r.status_code >= 400:
                return []
            return [_to_file_info(row) for row in r.json()]
        return super().glob_info(pattern, path)

    def write(self, path: str, content: str | bytes) -> WriteResult:
        self.touch()
        if self._mode == "http":
            try:
                raw = content if isinstance(content, bytes) else content.encode("utf-8")
                payload = base64.b64encode(raw).decode("ascii")
                r = self._http.post("/write", json={"path": path, "content_b64": payload})
                r.raise_for_status()
            except Exception as exc:
                return WriteResult(error=f"Error: {exc}")
            return WriteResult(path=path)
        return super().write(path, content)


def _detect_in_cluster() -> bool:
    return os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")


def _sanitize_pod_name(raw: str, *, prefix: str = "pab-sandbox-") -> str:
    """DNS-1123: lowercase, digits, hyphen; max 63 chars, must start+end alnum."""
    cleaned = "".join(c if (c.isalnum() or c == "-") else "-" for c in raw.lower())
    cleaned = cleaned.strip("-")
    if not cleaned:
        cleaned = uuid.uuid4().hex
    name = f"{prefix}{cleaned}"
    return name[:63].rstrip("-") or f"{prefix}{uuid.uuid4().hex[:8]}"


def _build_pod_body(
    *,
    name: str,
    image: str,
    namespace: str,
    port: int,
    mode: str,
    exec_token: str,
    extra_labels: dict[str, str],
    extra_env: dict[str, str],
    service_account_name: str,
    override: dict[str, Any] | None,
) -> dict[str, Any]:
    if override is not None:
        # Per the docstring, the override is "your spec, used verbatim
        # with image / env / labels / ports filled in". Honour that
        # contract: the user's pod spec wins on everything except the
        # bits the sandbox client itself needs to talk to the pod.
        body: dict[str, Any] = copy.deepcopy(override)
        meta = body.setdefault("metadata", {})
        meta["name"] = name
        meta.setdefault("namespace", namespace)
        labels = meta.setdefault("labels", {})
        labels.setdefault("app", "pab-sandbox")
        for k, v in extra_labels.items():
            labels.setdefault(k, v)

        spec = body.setdefault("spec", {})
        containers = spec.setdefault("containers", [{}])
        container = containers[0]
        container["image"] = image
        container.setdefault("name", "sandbox")

        env_list: list[dict[str, str]] = list(container.get("env") or [])
        env_names = {e.get("name") for e in env_list}
        for required in (
            {"name": "SANDBOX_EXEC_TOKEN", "value": exec_token},
            {"name": "SANDBOX_PORT", "value": str(port)},
            {"name": "PYTHONUNBUFFERED", "value": "1"},
        ):
            if required["name"] not in env_names:
                env_list.append(required)
        for k, v in extra_env.items():
            if k not in env_names:
                env_list.append({"name": k, "value": v})
        container["env"] = env_list

        if mode == "http":
            ports = container.setdefault("ports", [])
            if not any(p.get("containerPort") == port for p in ports):
                ports.append({"containerPort": port, "name": "exec"})
            container.setdefault(
                "readinessProbe",
                {
                    "httpGet": {"path": "/health", "port": port},
                    "initialDelaySeconds": 1,
                    "periodSeconds": 2,
                    "failureThreshold": 5,
                },
            )
        return body

    default_labels = {"app": "pab-sandbox", **extra_labels}
    default_env: list[dict[str, str]] = [
        {"name": "SANDBOX_EXEC_TOKEN", "value": exec_token},
        {"name": "SANDBOX_PORT", "value": str(port)},
        {"name": "PYTHONUNBUFFERED", "value": "1"},
    ]
    for k, v in extra_env.items():
        default_env.append({"name": k, "value": v})

    default_container: dict[str, Any] = {
        "name": "sandbox",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "env": default_env,
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
            {"name": "workspace", "mountPath": "/workspace"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
    }
    if mode == "http":
        default_container["ports"] = [{"containerPort": port, "name": "exec"}]
        default_container["readinessProbe"] = {
            "httpGet": {"path": "/health", "port": port},
            "initialDelaySeconds": 1,
            "periodSeconds": 2,
            "failureThreshold": 5,
        }

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": default_labels},
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
            "containers": [default_container],
            "volumes": [
                {"name": "workspace", "emptyDir": {"sizeLimit": "1Gi"}},
                {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
            ],
        },
    }


def _to_file_info(row: dict[str, Any]) -> FileInfo:
    return FileInfo(
        name=row.get("name", ""),
        path=row.get("path", ""),
        is_dir=bool(row.get("is_dir", False)),
        size=row.get("size"),
        # The in-pod server ships with the user's image, so an older one simply
        # does not send this yet.
        modified_at=row.get("modified_at"),
    )
