"""A Kubernetes pod as a Pydantic AI workspace."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import anyio
import anyio.to_thread
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef

from pydantic_ai_backends.backends.kubernetes import DEFAULT_WORK_DIR, KubernetesPodSandbox
from pydantic_ai_backends.workspaces._container import ContainerWorkspaceBackend, RunnerSandbox

KUBERNETES_PROVIDER = "kubernetes"
"""Default `WorkspaceRef.provider` of a pod workspace."""

PodFactory = Callable[[str], KubernetesPodSandbox]
"""Builds the pod sandbox for a sandbox id, without creating the pod."""


class KubernetesWorkspaceBackend(ContainerWorkspaceBackend):
    """One pod, as the environment an agent run works in.

    The first operation creates the pod and waits for it to be Ready; a ref
    attaches to a running pod and fails with `WorkspaceUnavailableError` when it
    is gone or has finished. The pod lives until :meth:`KubernetesWorkspace.destroy`.

    Args:
        pod_factory: Builds the `KubernetesPodSandbox` for a sandbox id. Holds
            the image, namespace and pod template, and must not create the pod.
        ref: The workspace to attach to; `None` creates one on first use.
        env: Variables every command gets, under any a call passes.
        provider: Provider name in refs; distinct per cluster when an agent
            can reach several.
    """

    def __init__(
        self,
        *,
        pod_factory: PodFactory,
        ref: WorkspaceRef | None = None,
        env: Mapping[str, str] | None = None,
        provider: str = KUBERNETES_PROVIDER,
    ) -> None:
        async def open_pod(sandbox_id: str | None) -> tuple[str, RunnerSandbox]:
            sandbox = pod_factory(sandbox_id or uuid.uuid4().hex)
            if sandbox_id is None:
                await anyio.to_thread.run_sync(sandbox.start)
            else:
                await anyio.to_thread.run_sync(sandbox.attach)
            return sandbox.id, sandbox

        super().__init__(provider=provider, opener=open_pod, ref=ref, env=env)


@dataclass(kw_only=True)
class KubernetesWorkspace(AbstractCapability[object]):
    """Supply a Kubernetes pod as the run's workspace.

    A run with no ref creates a pod; one carrying this capability's provider
    attaches to that pod. Pods are kept after the run — the ref is how to come
    back, and :meth:`destroy` deletes one. Commands go through `pods/exec`, so
    the caller needs that RBAC and the image needs `/bin/sh`.

    Not checked against a live cluster in this repository's CI.

    Example:
        ```python
        from pydantic_ai import Agent

        from pydantic_ai_backends import ConsoleCapability
        from pydantic_ai_backends.workspaces import KubernetesWorkspace

        pods = KubernetesWorkspace(image="python:3.12-slim", namespace="agents")
        agent = Agent("anthropic:claude-opus-5-5", capabilities=[pods, ConsoleCapability()])
        ```
    """

    image: str
    """Container image; needs `/bin/sh`."""

    namespace: str = "default"
    """Namespace the pods live in."""

    work_dir: str = DEFAULT_WORK_DIR
    """Directory commands start in."""

    pod_template: dict[str, Any] | None = None
    """A full pod spec instead of the hardened default; its first container must stay up."""

    kube_config_path: str | None = None
    """A kubeconfig; in-cluster config, then `~/.kube/config` when omitted."""

    service_account_name: str = "default"
    """The pods' service account; a dedicated one with no permissions is the right choice."""

    provider: str = KUBERNETES_PROVIDER
    """Provider name in refs; distinct per cluster when an agent can reach several."""

    env: Mapping[str, str] | None = field(default=None, repr=False)
    """Variables every command gets. Nothing is read from the host's environment."""

    def __post_init__(self) -> None:
        if self.defer_loading:
            raise UserError(
                "`KubernetesWorkspace` does not support `defer_loading=True`: "
                "the workspace is selected before deferred capabilities load."
            )

    def _pod(self, sandbox_id: str) -> KubernetesPodSandbox:
        return KubernetesPodSandbox(
            self.image,
            namespace=self.namespace,
            sandbox_id=sandbox_id,
            work_dir=self.work_dir,
            pod_template=self.pod_template,
            kube_config_path=self.kube_config_path,
            service_account_name=self.service_account_name,
        )

    def backend(self, ref: WorkspaceRef | None = None) -> KubernetesWorkspaceBackend:
        """A backend for `ref`, or for a new pod; no API call until its first operation."""
        return KubernetesWorkspaceBackend(
            pod_factory=self._pod, ref=ref, env=self.env, provider=self.provider
        )

    def get_workspace(
        self, ctx: RunContext[object], *, ref: WorkspaceRef | None
    ) -> WorkspaceBackend | None:
        """This run's backend, or `None` for a ref another provider owns."""
        del ctx
        if ref is not None and ref.provider != self.provider:
            return None
        return self.backend(ref)

    async def destroy(self, ref: WorkspaceRef) -> None:
        """Delete the pod `ref` names. Already gone is fine.

        Raises:
            ValueError: `ref` belongs to another provider.
        """
        if ref.provider != self.provider:
            raise ValueError(f"expected a {self.provider!r} workspace ref, got {ref.provider!r}")
        await anyio.to_thread.run_sync(self._pod(ref.id).stop)
