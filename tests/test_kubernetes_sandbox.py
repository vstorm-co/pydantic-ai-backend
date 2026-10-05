"""`KubernetesPodSandbox` and `KubernetesWorkspace`, against a fake API.

No cluster: the core API and the `pods/exec` stream are hand-rolled fakes
installed with `monkeypatch`. `tests/test_workspace_kubernetes_conformance.py`
runs Pydantic AI's conformance suite against a real cluster when one is reachable.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import anyio
import pytest
from kubernetes.client.rest import ApiException
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import WorkspaceRef, WorkspaceUnavailableError

from pydantic_ai_backends.backends import kubernetes as k8s
from pydantic_ai_backends.backends._runner import STOPPER
from pydantic_ai_backends.backends.kubernetes import (
    KubernetesPodSandbox,
    _build_pod_body,
    _sanitize_pod_name,
)
from pydantic_ai_backends.protocol import SandboxUnavailableError
from pydantic_ai_backends.workspaces import KubernetesWorkspace, KubernetesWorkspaceBackend


@dataclass
class _Condition:
    type: str
    status: str


@dataclass
class _Status:
    phase: str = "Running"
    conditions: list[_Condition] = field(
        default_factory=lambda: [_Condition(type="Ready", status="True")]
    )


@dataclass
class _Pod:
    status: _Status = field(default_factory=_Status)


class _Core:
    def __init__(self) -> None:
        self.pods: dict[str, _Pod] = {}
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.create_error: Exception | None = None
        self.read_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.phase_on_create = "Running"

    def connect_get_namespaced_pod_exec(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("reached only through the stream fake")

    def create_namespaced_pod(self, namespace: str, body: dict[str, Any]) -> None:
        if self.create_error is not None:
            raise self.create_error
        self.created.append(body)
        self.pods[body["metadata"]["name"]] = _Pod(_Status(phase=self.phase_on_create))

    def read_namespaced_pod(self, name: str, namespace: str) -> _Pod:
        if self.read_error is not None:
            raise self.read_error
        if name not in self.pods:
            raise ApiException(status=404, reason="Not Found")
        return self.pods[name]

    def delete_namespaced_pod(self, name: str, namespace: str, **kwargs: Any) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(name)
        self.pods.pop(name, None)


class _Stream:
    """A `pods/exec` stream: chunks, a status, and optionally a block until released."""

    def __init__(
        self,
        stdout: list[str] | None = None,
        stderr: list[str] | None = None,
        returncode: int | None = 0,
        hold: bool = False,
        update_error: Exception | None = None,
    ) -> None:
        self._stdout = list(stdout or [])
        self._stderr = list(stderr or [])
        self.returncode_value = returncode
        self.hold = hold
        self.released = threading.Event()
        self.update_error = update_error
        self.closed = False
        self.ran_forever = False

    def is_open(self) -> bool:
        if self.hold and not self.released.is_set():
            return True
        return bool(self._stdout or self._stderr)

    def update(self, timeout: float) -> None:
        if self.update_error is not None:
            raise self.update_error
        if self.hold and not self.released.is_set():
            self.released.wait(timeout)

    def peek_stdout(self) -> bool:
        return bool(self._stdout)

    def read_stdout(self) -> str:
        return self._stdout.pop(0)

    def peek_stderr(self) -> bool:
        return bool(self._stderr)

    def read_stderr(self) -> str:
        return self._stderr.pop(0)

    @property
    def returncode(self) -> int | None:
        return self.returncode_value

    def run_forever(self, timeout: float) -> None:
        self.ran_forever = True

    def close(self) -> None:
        self.closed = True


@dataclass
class _Cluster:
    core: _Core
    streams: list[_Stream] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    open_error: Exception | None = None

    def stream(
        self, fn: Any, name: str, namespace: str, *, command: list[str], **_: Any
    ) -> _Stream:
        self.commands.append(command)
        if self.open_error is not None:
            raise self.open_error
        if command[2] == STOPPER:
            for running in self.streams:
                running.released.set()
            return _Stream()
        return self.streams.pop(0)


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> _Cluster:
    import kubernetes.client
    import kubernetes.config
    import kubernetes.stream

    core = _Core()
    fake = _Cluster(core)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(kubernetes.config, "load_kube_config", lambda config_file=None: None)
    monkeypatch.setattr(kubernetes.config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(kubernetes.stream, "stream", fake.stream)
    monkeypatch.setattr(k8s.time, "sleep", lambda seconds: None)
    return fake


def _pod(cluster: _Cluster, **kwargs: Any) -> KubernetesPodSandbox:
    del cluster
    return KubernetesPodSandbox("python:3.12-slim", sandbox_id="abc", in_cluster=False, **kwargs)


class TestNames:
    def test_a_pod_name_is_dns_1123(self) -> None:
        name = _sanitize_pod_name("My_Session.ID" + "x" * 80)
        assert name.startswith("pab-sandbox-my-session-id") and len(name) <= 63

    def test_an_empty_id_gets_a_generated_name(self) -> None:
        assert len(_sanitize_pod_name("___")) > len("pab-sandbox-")

    def test_identity(self, cluster: _Cluster) -> None:
        pod = _pod(cluster, work_dir="/srv", idle_timeout=5)
        assert (pod.id, pod.pod_name, pod.work_dir, pod.idle_timeout) == (
            "abc",
            "pab-sandbox-abc",
            "/srv",
            5,
        )
        pod._last_activity = 0
        pod.touch()
        assert pod.last_activity > 0

    def test_an_id_is_generated(self, cluster: _Cluster) -> None:
        del cluster
        assert KubernetesPodSandbox("img", in_cluster=False).id


class TestConfig:
    def test_a_kubeconfig_path_and_in_cluster(self, cluster: _Cluster, monkeypatch) -> None:
        import kubernetes.config

        seen: list[object] = []
        monkeypatch.setattr(
            kubernetes.config, "load_kube_config", lambda config_file=None: seen.append(config_file)
        )
        monkeypatch.setattr(kubernetes.config, "load_incluster_config", lambda: seen.append("in"))
        KubernetesPodSandbox("img", kube_config_path="/k", in_cluster=False)
        KubernetesPodSandbox("img", in_cluster=True)
        assert seen == ["/k", "in"]

    def test_a_failing_load_is_reported(self, cluster: _Cluster, monkeypatch) -> None:
        import kubernetes.config

        def fail(config_file: str | None = None) -> None:
            raise OSError("no kubeconfig")

        monkeypatch.setattr(kubernetes.config, "load_kube_config", fail)
        with pytest.raises(RuntimeError, match="failed to load kubernetes config"):
            KubernetesPodSandbox("img", in_cluster=False)

    def test_in_cluster_is_detected(self, monkeypatch) -> None:
        monkeypatch.setattr(k8s.os.path, "exists", lambda path: True)
        assert k8s._detect_in_cluster() is True


class TestLifecycle:
    def test_start_creates_the_pod_and_waits_for_ready(self, cluster: _Cluster) -> None:
        pod = _pod(cluster)
        pod.start()
        assert cluster.core.created[0]["metadata"]["name"] == "pab-sandbox-abc"
        assert pod.is_alive()

    def test_a_failed_create_is_reported(self, cluster: _Cluster) -> None:
        cluster.core.create_error = ApiException(status=403, reason="Forbidden")
        with pytest.raises(RuntimeError, match="failed to create"):
            _pod(cluster).start()

    def test_a_pod_that_finishes_before_ready_is_removed(self, cluster: _Cluster) -> None:
        cluster.core.phase_on_create = "Failed"
        with pytest.raises(RuntimeError, match="did not start"):
            _pod(cluster).start()
        assert cluster.core.deleted == ["pab-sandbox-abc"]

    def test_a_pod_never_ready_times_out_and_is_removed(self, cluster: _Cluster) -> None:
        cluster.core.phase_on_create = "Pending"
        with pytest.raises(RuntimeError, match="not ready"):
            _pod(cluster, startup_timeout=0).start()
        assert cluster.core.deleted == ["pab-sandbox-abc"]

    def test_start_waits_while_the_pod_is_pending(self, cluster: _Cluster, monkeypatch) -> None:
        cluster.core.phase_on_create = "Pending"
        pod = _pod(cluster)

        def become_ready(seconds: float) -> None:
            cluster.core.pods[pod.pod_name].status = _Status()

        monkeypatch.setattr(k8s.time, "sleep", become_ready)
        pod.start()
        assert pod.is_alive()

    def test_attach(self, cluster: _Cluster) -> None:
        pod = _pod(cluster)
        with pytest.raises(SandboxUnavailableError, match="does not exist"):
            pod.attach()
        pod.start()
        pod.attach()
        cluster.core.pods[pod.pod_name].status.phase = "Succeeded"
        with pytest.raises(SandboxUnavailableError, match="finished"):
            pod.attach()
        cluster.core.pods[pod.pod_name].status = _Status(phase="Pending", conditions=[])
        with pytest.raises(SandboxUnavailableError, match="not running"):
            pod.attach()

    def test_liveness(self, cluster: _Cluster) -> None:
        pod = _pod(cluster)
        assert pod.is_alive() is False
        cluster.core.read_error = ApiException(status=500, reason="boom")
        assert pod.is_alive() is False
        with pytest.raises(ApiException):
            pod.attach()

    def test_stop_deletes_quietly(self, cluster: _Cluster) -> None:
        pod = _pod(cluster)
        pod.start()
        pod.stop(purge=True)
        cluster.core.delete_error = ApiException(status=500, reason="boom")
        pod.stop()
        assert cluster.core.deleted == ["pab-sandbox-abc"]


class TestRunCommand:
    async def test_streams_come_back_apart_with_the_status(self, cluster: _Cluster) -> None:
        cluster.streams.append(_Stream(stdout=["out"], stderr=["err"], returncode=3))
        pod = _pod(cluster)
        outcome = await pod.run_command(["prog", "a b"], run_id="r1", env={"K": "V"})
        assert (outcome.stdout, outcome.stderr, outcome.exit_code) == ("out", "err", 3)
        command = cluster.commands[0]
        assert command[4:7] == ["/workspace", "env", "K=V"]
        assert command[-2:] == ["prog", "a b"]

    async def test_no_status_means_the_pod_went_away(self, cluster: _Cluster) -> None:
        cluster.streams.append(_Stream(returncode=None))
        with pytest.raises(SandboxUnavailableError):
            await _pod(cluster).run_command(["true"], run_id="r1")

    @pytest.mark.parametrize("alive", [True, False])
    async def test_a_signal_exit_is_a_result_only_in_a_live_pod(
        self, cluster: _Cluster, alive: bool
    ) -> None:
        pod = _pod(cluster)
        if alive:
            pod.start()
        cluster.streams.append(_Stream(returncode=137))
        if alive:
            assert (await pod.run_command(["sleep"], run_id="r1")).exit_code == 137
        else:
            with pytest.raises(SandboxUnavailableError):
                await pod.run_command(["sleep"], run_id="r1")

    async def test_a_deadline_stops_the_command(self, cluster: _Cluster) -> None:
        cluster.streams.append(_Stream(stdout=["partial"], hold=True))
        outcome = await _pod(cluster).run_command(["sleep", "9"], run_id="r1", timeout=0.3)
        assert (outcome.timed_out, outcome.stdout) == (True, "partial")
        assert any(command[2] == STOPPER for command in cluster.commands)

    async def test_output_over_the_limit_stops_the_command(self, cluster: _Cluster) -> None:
        cluster.streams.append(_Stream(stdout=["12345", "never read"]))
        outcome = await _pod(cluster).run_command(["yes"], run_id="r1", output_limit=4)
        assert (outcome.output_limited, outcome.stdout) == (True, "12345")

    async def test_cancellation_stops_the_command(self, cluster: _Cluster) -> None:
        cluster.streams.append(_Stream(hold=True))
        with anyio.move_on_after(0.3):
            await _pod(cluster).run_command(["sleep", "9"], run_id="r1")
        assert any(command[2] == STOPPER for command in cluster.commands)

    async def test_a_pod_that_is_gone_when_the_stream_opens(self, cluster: _Cluster) -> None:
        cluster.open_error = ApiException(status=404, reason="Not Found")
        with pytest.raises(SandboxUnavailableError, match="is gone"):
            await _pod(cluster).run_command(["true"], run_id="r1")

    async def test_other_api_errors_propagate(self, cluster: _Cluster) -> None:
        cluster.open_error = ApiException(status=500, reason="Internal")
        with pytest.raises(ApiException):
            await _pod(cluster).run_command(["true"], run_id="r1")

    async def test_a_broken_stream_is_translated(self, cluster: _Cluster) -> None:
        cluster.streams.append(
            _Stream(stdout=["x"], update_error=ApiException(status=410, reason="Gone"))
        )
        with pytest.raises(SandboxUnavailableError):
            await _pod(cluster).run_command(["true"], run_id="r1")

    async def test_an_empty_argv_is_refused(self, cluster: _Cluster) -> None:
        with pytest.raises(ValueError, match="name a program"):
            await _pod(cluster).run_command([], run_id="r1")

    async def test_a_failing_stop_is_quiet(self, cluster: _Cluster) -> None:
        cluster.open_error = RuntimeError("api down")
        await _pod(cluster).stop_command("r1")


class TestPodBody:
    def test_the_default_is_hardened_and_stays_up(self) -> None:
        body = _build_pod_body(
            name="p",
            image="img",
            namespace="ns",
            work_dir="/w",
            extra_labels={"team": "a"},
            extra_env={"X": "1"},
            service_account_name="sa",
            override=None,
        )
        container = body["spec"]["containers"][0]
        assert container["command"] == ["sleep", "infinity"]
        assert container["workingDir"] == "/w"
        assert {"name": "X", "value": "1"} in container["env"]
        assert body["metadata"]["labels"] == {"app": "pab-sandbox", "team": "a"}
        assert container["securityContext"]["allowPrivilegeEscalation"] is False

    def test_an_override_wins_except_for_what_the_sandbox_needs(self) -> None:
        override = {
            "metadata": {"labels": {"team": "mine"}},
            "spec": {"containers": [{"env": [{"name": "X", "value": "keep"}]}]},
        }
        body = _build_pod_body(
            name="p",
            image="img",
            namespace="ns",
            work_dir="/w",
            extra_labels={"team": "theirs", "extra": "1"},
            extra_env={"X": "override", "Y": "2"},
            service_account_name="sa",
            override=override,
        )
        container = body["spec"]["containers"][0]
        assert (container["image"], container["name"]) == ("img", "sandbox")
        assert container["env"] == [{"name": "X", "value": "keep"}, {"name": "Y", "value": "2"}]
        assert body["metadata"]["labels"] == {"team": "mine", "app": "pab-sandbox", "extra": "1"}
        assert override["metadata"] == {"labels": {"team": "mine"}}


class TestKubernetesWorkspace:
    async def test_creates_then_attaches_by_ref(self, cluster: _Cluster) -> None:
        capability = KubernetesWorkspace(image="img", env={"A": "1"})
        backend = capability.backend()
        assert await backend.working_dir() == "/workspace"
        assert backend.ref is not None and backend.ref.provider == "kubernetes"
        assert len(cluster.core.created) == 1

        cluster.streams.append(_Stream(stdout=["ok"]))
        attached = capability.backend(backend.ref)
        result = await attached.run(["true"])
        assert result.stdout == "ok"
        assert len(cluster.core.created) == 1
        assert "A=1" in cluster.commands[-1]

    async def test_a_ref_whose_pod_is_gone_is_unavailable(self, cluster: _Cluster) -> None:
        del cluster
        backend = KubernetesWorkspace(image="img").backend(
            WorkspaceRef(provider="kubernetes", id="gone")
        )
        with pytest.raises(WorkspaceUnavailableError, match="does not exist"):
            await backend.working_dir()

    async def test_a_pod_lost_mid_command_is_unavailable(self, cluster: _Cluster) -> None:
        backend = KubernetesWorkspace(image="img").backend()
        await backend.working_dir()
        cluster.open_error = ApiException(status=404, reason="Not Found")
        with pytest.raises(WorkspaceUnavailableError):
            await backend.run(["true"])

    async def test_destroy_deletes_the_pod(self, cluster: _Cluster) -> None:
        capability = KubernetesWorkspace(image="img")
        backend = capability.backend()
        await backend.working_dir()
        assert backend.ref is not None
        await capability.destroy(backend.ref)
        assert cluster.core.deleted == [f"pab-sandbox-{backend.ref.id}"]

    async def test_another_providers_ref_is_refused(self, cluster: _Cluster) -> None:
        del cluster
        capability = KubernetesWorkspace(image="img", provider="kubernetes:eu")
        ctx: Any = None
        assert (
            capability.get_workspace(ctx, ref=WorkspaceRef(provider="kubernetes", id="x")) is None
        )
        assert isinstance(capability.get_workspace(ctx, ref=None), KubernetesWorkspaceBackend)
        with pytest.raises(ValueError, match="'kubernetes:eu' workspace ref"):
            await capability.destroy(WorkspaceRef(provider="docker", id="x"))
        with pytest.raises(ValueError, match="'kubernetes:eu' workspace ref"):
            capability.backend(WorkspaceRef(provider="docker", id="x"))

    def test_deferred_loading_is_refused(self) -> None:
        with pytest.raises(UserError, match="defer_loading"):
            KubernetesWorkspace(image="img", defer_loading=True)
