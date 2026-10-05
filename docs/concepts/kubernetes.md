# Kubernetes

`KubernetesWorkspace` gives an agent a pod as its [workspace](workspaces.md). Commands
reach the pod through the API's `pods/exec` subresource, so the image needs `/bin/sh` and
nothing else of ours, and the caller needs `pods/exec` RBAC on the namespace.

!!! warning "Not checked against a live cluster in CI"
    The unit tests drive a fake API. `tests/test_workspace_kubernetes_conformance.py` runs
    Pydantic AI's conformance suite against a real cluster:
    `uv run pytest -m kubernetes tests/test_workspace_kubernetes_conformance.py`.

```bash
pip install "pydantic-ai-backend[console,kubernetes]"
```

## When to choose it

You already run agents next to a cluster, want sandboxes scheduled, limited and isolated by
it, and do not want a Docker socket anywhere. For a single host, [`DockerWorkspace`](docker.md)
or [`sandboxd`](remote.md) is less to operate.

## Basic Usage

```python
from pydantic_ai import Agent

from pydantic_ai_backends import ConsoleCapability, KubernetesWorkspace

pods = KubernetesWorkspace(image="python:3.12-slim", namespace="agents")
agent = Agent("anthropic:claude-opus-5-5", capabilities=[pods, ConsoleCapability()])
```

The first operation creates a pod named `pab-sandbox-…`, waits for it to be Ready, and
records its id as the run's ref; a later run attaches to it, and a pod that is gone or has
finished fails with `WorkspaceUnavailableError`. `destroy(ref)` deletes it.

## How a command runs

stdout and stderr arrive on their own channels and the exit status on a third. The
deadline is kept by the client, and a command whose caller times out or is cancelled is
stopped by a second exec that signals its process group — the same small `sh` wrapper the
Docker workspace uses. `pods/exec` takes no working directory and no environment, so the
wrapper moves to `work_dir` and `env` sets the variables.

## The pod

The default pod is hardened: non-root (uid 1000), a read-only root filesystem with
`emptyDir` volumes at `work_dir` and `/tmp`, all capabilities dropped, a seccomp profile,
no service-account token mounted, and a `sleep infinity` command so the container stays up
for commands to be exec'd into it. `pod_template` replaces it with your own spec; the
name, image and labels are filled in, and its first container must stay running.

| Field | Default | |
|---|---|---|
| `image` | — | Needs `/bin/sh` |
| `namespace` | `"default"` | |
| `work_dir` | `"/workspace"` | Where commands start |
| `pod_template` | `None` | A full pod spec instead of the default |
| `kube_config_path` | `None` | In-cluster config, then `~/.kube/config` |
| `service_account_name` | `"default"` | A dedicated one with no permissions is the right choice |
| `provider` | `"kubernetes"` | Distinct per cluster when an agent can reach several |
| `env` | `None` | Variables every command gets |

## What changed from `KubernetesPodSandbox`

The `mode="http"` path, which talked to an exec server baked into the image, is gone: that
server folded stdout and stderr into one stream and could not stop a command its caller
gave up on. `pods/exec` does both.
