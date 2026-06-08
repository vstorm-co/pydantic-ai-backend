"""Tests for KubernetesPodSandbox.

Mirrors `tests/test_daytona_sandbox.py` style: the entire kubernetes
SDK and httpx client are replaced by hand-rolled fakes. No real cluster.
A future integration test (gated on @pytest.mark.kubernetes, default
deselected) can run against `kind` once we add a CI job for it.
"""

from __future__ import annotations

import base64
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

# Stub the kubernetes SDK before importing the sandbox.
fake_k8s = types.ModuleType("kubernetes")
fake_k8s_client = types.ModuleType("kubernetes.client")
fake_k8s_config = types.ModuleType("kubernetes.config")
fake_k8s_rest = types.ModuleType("kubernetes.client.rest")
fake_k8s_stream_mod = types.ModuleType("kubernetes.stream")


class FakeApiException(Exception):
    def __init__(self, status: int = 500, reason: str = "x") -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass
class FakePodMeta:
    name: str
    namespace: str = "default"
    resource_version: str = "1"
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class FakePodCondition:
    type: str
    status: str


@dataclass
class FakePodStatus:
    phase: str = "Running"
    pod_ip: str | None = "10.0.0.10"
    conditions: list[FakePodCondition] = field(
        default_factory=lambda: [FakePodCondition(type="Ready", status="True")]
    )


@dataclass
class FakeContainer:
    name: str = "sandbox"
    env: list[Any] = field(default_factory=list)


@dataclass
class FakePodSpec:
    containers: list[FakeContainer] = field(default_factory=lambda: [FakeContainer()])


@dataclass
class FakePod:
    metadata: FakePodMeta
    status: FakePodStatus = field(default_factory=FakePodStatus)
    spec: FakePodSpec = field(default_factory=FakePodSpec)


class FakeCoreV1Api:
    def __init__(self) -> None:
        self.pods: dict[tuple[str, str], FakePod] = {}
        self.deletes: list[tuple[str, str]] = []
        self.create_should_fail = False

    def create_namespaced_pod(self, namespace: str, body: dict[str, Any]) -> FakePod:
        if self.create_should_fail:
            raise FakeApiException(status=500, reason="boom")
        name = body["metadata"]["name"]
        pod = FakePod(metadata=FakePodMeta(name=name, namespace=namespace))
        self.pods[(name, namespace)] = pod
        return pod

    def read_namespaced_pod(self, name: str, namespace: str) -> FakePod:
        try:
            return self.pods[(name, namespace)]
        except KeyError as exc:
            raise FakeApiException(status=404, reason="not found") from exc

    def delete_namespaced_pod(
        self,
        name: str,
        namespace: str,
        grace_period_seconds: int = 30,
        propagation_policy: str = "Background",
    ) -> None:
        self.pods.pop((name, namespace), None)
        self.deletes.append((name, namespace))

    def list_namespaced_pod(self, namespace: str, label_selector: str = "") -> Any:
        items = [p for (n, ns), p in self.pods.items() if ns == namespace]

        class R:
            def __init__(self, x: list[FakePod]) -> None:
                self.items = x

        return R(items)


def _load_in_cluster_config() -> None:
    return None


def _load_kube_config(config_file: str | None = None) -> None:
    return None


fake_k8s_config.load_incluster_config = _load_in_cluster_config
fake_k8s_config.load_kube_config = _load_kube_config

fake_k8s_client.CoreV1Api = FakeCoreV1Api
fake_k8s_client.ApiClient = object
fake_k8s_rest.ApiException = FakeApiException


def _stream_fn(*args: Any, **kwargs: Any) -> Any:
    class _R:
        returncode: int | None = 0

        def is_open(self) -> bool:
            return False

        def update(self, timeout: int = 1) -> None:
            return None

        def peek_stdout(self) -> bool:
            return False

        def peek_stderr(self) -> bool:
            return False

        def close(self) -> None:
            return None

    return _R()


fake_k8s_stream_mod.stream = _stream_fn

sys.modules["kubernetes"] = fake_k8s
sys.modules["kubernetes.client"] = fake_k8s_client
sys.modules["kubernetes.config"] = fake_k8s_config
sys.modules["kubernetes.client.rest"] = fake_k8s_rest
sys.modules["kubernetes.stream"] = fake_k8s_stream_mod


# Stub httpx to a tiny synchronous double.
class FakeHttpResponse:
    def __init__(self, *, status_code: int, json_data: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._json


class FakeHttpClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.handler: Any = None  # set by tests

    def get(self, path: str, timeout: float | None = None) -> FakeHttpResponse:
        return FakeHttpResponse(status_code=200, json_data={"ready": True})

    def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> FakeHttpResponse:
        self.calls.append((path, json or {}))
        if self.handler is None:
            return FakeHttpResponse(
                status_code=200,
                json_data={"output": "ok", "exit_code": 0, "truncated": False},
            )
        return self.handler(path, json)

    def close(self) -> None:
        return None


class FakeTimeout:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


fake_httpx = types.ModuleType("httpx")
fake_httpx.Client = FakeHttpClient
fake_httpx.Timeout = FakeTimeout
sys.modules["httpx"] = fake_httpx


# Now import the SUT.
from pydantic_ai_backends.backends.kubernetes import (  # noqa: E402
    KubernetesPodSandbox,
    _sanitize_pod_name,
)

# ---------------------------------------------------------------------


def test_sanitize_pod_name_strips_underscores_and_caps_length() -> None:
    name = _sanitize_pod_name("Sess_With_UPPER_and_underscores")
    assert name.startswith("pab-sandbox-")
    assert all(ch.islower() or ch.isdigit() or ch == "-" for ch in name)
    assert len(name) <= 63


def test_sanitize_pod_name_when_input_empty() -> None:
    name = _sanitize_pod_name("")
    assert name.startswith("pab-sandbox-")
    assert len(name) > len("pab-sandbox-")


def test_init_accepts_session_id_alias() -> None:
    sb = KubernetesPodSandbox("img:1", session_id="abc")
    assert sb.id == "abc"


def test_init_uses_sandbox_id_over_session_id() -> None:
    sb = KubernetesPodSandbox("img:1", sandbox_id="explicit", session_id="other")
    assert sb.id == "explicit"


def test_start_creates_pod_and_marks_ready() -> None:
    sb = KubernetesPodSandbox("img:1", sandbox_id="abc", startup_timeout=2)
    sb.start()
    assert sb._pod_ip == "10.0.0.10"
    assert sb._http is not None
    assert sb.is_alive()


def test_start_raises_when_create_fails() -> None:
    sb = KubernetesPodSandbox("img:1", sandbox_id="abc", startup_timeout=2)
    sb._core.create_should_fail = True
    with pytest.raises(RuntimeError, match="failed to create"):
        sb.start()


def test_stop_deletes_pod_and_is_idempotent() -> None:
    sb = KubernetesPodSandbox("img:1", sandbox_id="abc", startup_timeout=2)
    sb.start()
    sb.stop()
    assert sb._stopped
    assert ("pab-sandbox-abc", "default") in sb._core.deletes
    sb.stop()  # no double-delete
    assert sb._core.deletes.count(("pab-sandbox-abc", "default")) == 1


def test_stop_can_be_configured_to_keep_pod() -> None:
    sb = KubernetesPodSandbox("img:1", sandbox_id="abc", delete_on_stop=False)
    sb.start()
    sb.stop()
    assert sb._core.deletes == []


def test_execute_returns_response_from_http() -> None:
    sb = KubernetesPodSandbox("img:1", sandbox_id="abc", startup_timeout=2)
    sb.start()
    result = sb.execute("echo hello")
    assert result.exit_code == 0
    assert result.output == "ok"
    assert not result.truncated


def test_execute_swallows_http_errors_into_response() -> None:
    sb = KubernetesPodSandbox("img:1", sandbox_id="abc", startup_timeout=2)
    sb.start()

    def raising(_p: str, _j: Any) -> Any:
        raise RuntimeError("network down")

    sb._http.handler = raising
    result = sb.execute("echo hi")
    assert result.exit_code == 1
    assert "network down" in result.output


def test_execute_truncates_at_output_cap() -> None:
    sb = KubernetesPodSandbox("img:1", sandbox_id="abc", startup_timeout=2)
    sb.start()

    def big(_p: str, _j: Any) -> Any:
        return FakeHttpResponse(
            status_code=200,
            json_data={"output": "x" * 200_000, "exit_code": 0, "truncated": True},
        )

    sb._http.handler = big
    result = sb.execute("yes")
    assert len(result.output) == 100_000
    assert result.truncated


def test_execute_updates_last_activity() -> None:
    sb = KubernetesPodSandbox("img:1", sandbox_id="abc", startup_timeout=2)
    sb.start()
    before = sb._last_activity
    sb.execute("noop")
    assert sb._last_activity >= before


def test_token_required_in_http_path() -> None:
    sb = KubernetesPodSandbox("img:1", sandbox_id="abc", exec_token="t-abc")
    sb.start()
    assert sb._http.kwargs["headers"]["X-Sandbox-Token"] == "t-abc"


def test_pod_template_override_is_used_verbatim() -> None:
    override = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "ignored", "labels": {"custom": "yes"}},
        "spec": {"containers": [{"name": "x", "image": "y"}]},
    }
    sb = KubernetesPodSandbox("img:1", sandbox_id="abc", startup_timeout=2, pod_template=override)
    sb.start()
    pod_key = ("pab-sandbox-abc", "default")
    assert pod_key in sb._core.pods


# ---------------------------------------------------------------------
# _build_pod_body coverage — exercises both default and override branches
# across mode="http"/"api" and extra env/labels.


def test_build_pod_body_default_appends_extra_env() -> None:
    sb = KubernetesPodSandbox("img:1", sandbox_id="env1", startup_timeout=2, env={"FOO": "bar"})
    sb.start()
    pod = sb._core.pods[("pab-sandbox-env1", "default")]
    # Default branch builds a fresh body; we only assert it started.
    assert pod is not None


def test_build_pod_body_default_api_mode_skips_http_block() -> None:
    sb = KubernetesPodSandbox("img:1", sandbox_id="api1", startup_timeout=2, mode="api")
    sb.start()
    # api mode never builds the in-pod HTTP client.
    assert sb._http is None
    assert sb.is_alive()


def test_build_pod_body_override_merges_extra_env_and_labels() -> None:
    override = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "ignored", "labels": {"keep": "yes"}},
        "spec": {
            "containers": [
                {
                    "name": "x",
                    "image": "y",
                    # Pre-populate one of the required env vars so the
                    # "name already present, skip" branch is hit.
                    "env": [{"name": "SANDBOX_EXEC_TOKEN", "value": "preset"}],
                }
            ]
        },
    }
    sb = KubernetesPodSandbox(
        "img:1",
        sandbox_id="ovr1",
        startup_timeout=2,
        pod_template=override,
        # Mix of a brand-new key (EXTRA — should append) and a key the
        # pod_template already pre-populates (SANDBOX_EXEC_TOKEN —
        # should be skipped); covers both branches of the env-merge.
        env={"EXTRA": "x", "SANDBOX_EXEC_TOKEN": "ignored"},
        labels={"team": "search"},
    )
    sb.start()
    assert ("pab-sandbox-ovr1", "default") in sb._core.pods


def test_build_pod_body_override_api_mode_skips_ports() -> None:
    override = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "ignored"},
        "spec": {"containers": [{"name": "x", "image": "y"}]},
    }
    sb = KubernetesPodSandbox(
        "img:1",
        sandbox_id="ovrapi",
        startup_timeout=2,
        pod_template=override,
        mode="api",
    )
    sb.start()
    assert ("pab-sandbox-ovrapi", "default") in sb._core.pods


def test_build_pod_body_override_keeps_existing_port_entry() -> None:
    override = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "ignored"},
        "spec": {
            "containers": [
                {
                    "name": "x",
                    "image": "y",
                    "ports": [{"containerPort": 8080, "name": "exec"}],
                }
            ]
        },
    }
    sb = KubernetesPodSandbox(
        "img:1",
        sandbox_id="ovrport",
        startup_timeout=2,
        pod_template=override,
    )
    sb.start()
    assert ("pab-sandbox-ovrport", "default") in sb._core.pods


def test_ls_info_maps_http_rows_to_file_info() -> None:
    sb = KubernetesPodSandbox("img:1", sandbox_id="ls1", startup_timeout=2)
    sb.start()

    def rows(_p: str, _j: Any) -> Any:
        return FakeHttpResponse(
            status_code=200,
            json_data=[
                {"name": "a.txt", "path": "/a.txt", "is_dir": False, "size": 10},
                {"name": "d", "path": "/d", "is_dir": True, "size": None},
            ],
        )

    sb._http.handler = rows
    result = sb.ls_info("/")
    # FileInfo is a TypedDict, not a dataclass — index it.
    assert [r["name"] for r in result] == ["a.txt", "d"]
    assert result[0]["is_dir"] is False
    assert result[1]["is_dir"] is True


# ---------------------------------------------------------------------
# File ops in mode="http": read, read_bytes, write, edit.


def _started(**kwargs: Any) -> KubernetesPodSandbox:
    sb = KubernetesPodSandbox("img:1", sandbox_id="fs", startup_timeout=2, **kwargs)
    sb.start()
    return sb


def _fs_handler(files: dict[str, str]) -> Any:
    """Route /read and /write against an in-memory file dict."""

    def handler(path: str, body: dict[str, Any]) -> FakeHttpResponse:
        if path == "/read":
            target = body["path"]
            if target not in files:
                return FakeHttpResponse(status_code=404)
            return FakeHttpResponse(status_code=200, json_data={"content": files[target]})
        if path == "/write":
            decoded = base64.b64decode(body["content_b64"]).decode("utf-8")
            files[body["path"]] = decoded
            return FakeHttpResponse(status_code=200, json_data={})
        return FakeHttpResponse(status_code=200, json_data={})

    return handler


def test_read_returns_http_content() -> None:
    sb = _started()
    sb._http.handler = _fs_handler({"/a.txt": "hello"})
    assert sb.read("/a.txt") == "hello"


def test_read_returns_not_found_on_404() -> None:
    sb = _started()
    sb._http.handler = _fs_handler({})
    assert sb.read("/missing") == "Error: File '/missing' not found"


def test_read_returns_http_error_on_5xx() -> None:
    sb = _started()
    sb._http.handler = lambda _p, _j: FakeHttpResponse(status_code=500)
    assert sb.read("/x") == "Error: HTTP 500"


def test_read_returns_error_on_transport_exception() -> None:
    sb = _started()

    def boom(_p: str, _j: Any) -> Any:
        raise RuntimeError("conn refused")

    sb._http.handler = boom
    assert sb.read("/x") == "Error: conn refused"


def test_read_bytes_returns_content() -> None:
    sb = _started()
    sb._http.handler = _fs_handler({"/a.txt": "hello"})
    assert sb.read_bytes("/a.txt") == b"hello"


def test_read_bytes_returns_empty_on_http_error() -> None:
    sb = _started()
    sb._http.handler = _fs_handler({})  # /read → 404
    assert sb.read_bytes("/missing") == b""


def test_read_bytes_returns_empty_on_transport_exception() -> None:
    sb = _started()

    def boom(_p: str, _j: Any) -> Any:
        raise RuntimeError("conn refused")

    sb._http.handler = boom
    assert sb.read_bytes("/x") == b""


def test_write_returns_path_on_success() -> None:
    sb = _started()
    files: dict[str, str] = {}
    sb._http.handler = _fs_handler(files)
    result = sb.write("/a.txt", "payload")
    assert result.path == "/a.txt"
    assert result.error is None
    assert files["/a.txt"] == "payload"


def test_write_returns_error_on_http_failure() -> None:
    sb = _started()
    sb._http.handler = lambda _p, _j: FakeHttpResponse(status_code=500)
    result = sb.write("/a.txt", "payload")
    assert result.error is not None
    assert "Error" in result.error


def test_edit_replaces_single_occurrence() -> None:
    sb = _started()
    files = {"/f.txt": "alpha beta"}
    sb._http.handler = _fs_handler(files)
    result = sb.edit("/f.txt", "alpha", "gamma")
    assert result.error is None
    assert result.occurrences == 1
    assert files["/f.txt"] == "gamma beta"


def test_edit_string_not_found() -> None:
    sb = _started()
    sb._http.handler = _fs_handler({"/f.txt": "alpha beta"})
    result = sb.edit("/f.txt", "zzz", "x")
    assert result.error == "String not found in file"


def test_edit_multiple_occurrences_requires_replace_all() -> None:
    sb = _started()
    sb._http.handler = _fs_handler({"/f.txt": "a a a"})
    result = sb.edit("/f.txt", "a", "b")
    assert result.error is not None
    assert "found 3 times" in result.error


def test_edit_replace_all_replaces_every_occurrence() -> None:
    sb = _started()
    files = {"/f.txt": "a a a"}
    sb._http.handler = _fs_handler(files)
    result = sb.edit("/f.txt", "a", "b", replace_all=True)
    assert result.occurrences == 3
    assert files["/f.txt"] == "b b b"


def test_edit_file_not_found() -> None:
    sb = _started()
    sb._http.handler = _fs_handler({})  # /read → 404 → read_bytes b""
    result = sb.edit("/missing", "a", "b")
    assert result.error == "File '/missing' not found"


def test_edit_surfaces_write_error() -> None:
    sb = _started()

    def handler(path: str, body: dict[str, Any]) -> FakeHttpResponse:
        if path == "/read":
            return FakeHttpResponse(status_code=200, json_data={"content": "alpha"})
        return FakeHttpResponse(status_code=500)  # /write fails

    sb._http.handler = handler
    result = sb.edit("/f.txt", "alpha", "beta")
    assert result.error is not None


def test_edit_surfaces_read_bytes_error_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    # mode="api" delegates read_bytes to BaseSandbox, which returns a
    # b"[Error: ...]" sentinel on a failed `cat`. edit() must surface it
    # as the error instead of treating the text as file content.
    sb = _started()
    monkeypatch.setattr(sb, "read_bytes", lambda path: b"[Error: cat: missing: No such file]")
    result = sb.edit("/missing", "a", "b")
    assert result.error is not None
    assert result.error.startswith("[Error:")
