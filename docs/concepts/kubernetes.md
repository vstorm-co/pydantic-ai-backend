# Kubernetes Pod Sandbox

Run agent tools inside ephemeral Kubernetes pods, one per session. The
`KubernetesPodSandbox` is the third sandbox shipped by
`pydantic-ai-backend`, alongside `DockerSandbox` (host Docker daemon)
and `DaytonaSandbox` (managed cloud).

!!! warning "Requires the `kubernetes` extra"
    Install with `pip install pydantic-ai-backend[kubernetes]`. The
    `kubernetes` and `httpx` Python packages are pulled in.

## When to choose it

- **DockerSandbox** — local development, single-machine deployments,
  one process owns one Docker daemon.
- **DaytonaSandbox** — managed multi-tenant SaaS, you don't run any
  infra.
- **KubernetesPodSandbox** — you run Kubernetes already (managed or
  self-hosted), want hard ResourceQuota / NetworkPolicy isolation, or
  need warm-pool topology to keep cold-start latency low.

## Two execution modes

### `mode="http"` (default, recommended)

The pod image runs a small HTTP server (`/exec`, `/read`, `/write`,
`/edit`, `/grep`, `/glob`, `/ls`, `/health`) on a chosen port.
Authentication: the sandbox sends an `X-Sandbox-Token` header on every
request; the token is auto-generated per pod and passed to the
container as `SANDBOX_EXEC_TOKEN`.

**Why not `kubectl exec`:** the K8s `pods/exec` subresource truncates
long output streams under load and stalls on slow producers. HTTP
keep-alive on a tight cluster network is far more reliable for tools
that emit megabytes of output (npm install, headless browsers, MCP
servers).

A reference image is in
[examples/kubernetes-sandbox](../examples/kubernetes-sandbox.md).

### `mode="api"` (fallback)

Uses the Kubernetes `pods/exec` subresource directly. No in-pod HTTP
server required, but the calling identity needs `pods/exec` RBAC and
output is capped harder. Fine for short, deterministic commands.

**Image compatibility:** because `mode="api"` only needs a shell in
the container, **any image you already use with `DockerSandbox` works
as-is** — `python:3.12-slim`, `node:20-bookworm`, your custom runtime
image, anything. No HTTP server layer, no `SANDBOX_EXEC_TOKEN` env
var, no port exposure. Same image, swap the sandbox class:

!!! warning "Requires `/bin/sh` and `timeout`"
    `mode="api"` runs every command as `timeout <n> sh -c <command>`, so
    the image must ship `/bin/sh` **and** a `timeout` binary (GNU
    coreutils or the BusyBox applet). Stock `python:*`, `node:*`,
    `debian`, `ubuntu` and `alpine` all include it. A minimal or
    `distroless` image with no shell will fail with `timeout: not found`
    (surfaced as `ExecuteResponse(exit_code=1)`); use `mode="http"` with
    a purpose-built image for those.

```python
# Was: DockerSandbox(image="python:3.12-slim")
sandbox = KubernetesPodSandbox(
    image="python:3.12-slim",
    namespace="agents",
    mode="api",
)
```

For `mode="http"` the image must additionally run the in-pod exec
server (see [examples/kubernetes-sandbox](../examples/kubernetes-sandbox.md)).

## Basic usage with pydantic-ai

```python
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import KubernetesPodSandbox
from pydantic_ai_backends.toolsets.console import create_console_toolset


@dataclass
class Deps:
    sandbox: KubernetesPodSandbox


sandbox = KubernetesPodSandbox(
    image="ghcr.io/your-org/agent-sandbox:1.4",
    namespace="agents",
    mode="http",
    sandbox_id="user-42",
)
sandbox.start()

try:
    agent = Agent[Deps, str](
        "openai:gpt-4o",
        deps_type=Deps,
        toolsets=[create_console_toolset()],
    )
    result = await agent.run(
        "Install ripgrep in /workspace and grep TODO in /repo",
        deps=Deps(sandbox=sandbox),
    )
    print(result.output)
finally:
    sandbox.stop()  # deletes the pod
```

## Configuration

| Argument                | Default       | Description |
| ----------------------- | ------------- | ----------- |
| `image`                 | required      | Container image. Must expose port `port` for `mode="http"`. |
| `namespace`             | `"default"`   | Namespace to create the pod in. |
| `sandbox_id` / `session_id` | uuid4    | Identifier; sanitized to DNS-1123 and used as the pod name suffix. |
| `mode`                  | `"http"`      | `"http"` or `"api"`. |
| `port`                  | `8080`        | In-pod exec server port (`mode="http"`). |
| `exec_token`            | random        | Shared HMAC-style token for the in-pod server. |
| `pod_template`          | `None`        | Full pod spec dict; if set, used verbatim (with `image`/env merged in). |
| `kube_config_path`      | `None`        | Path to a kubeconfig. Falls back to in-cluster, then `~/.kube/config`. |
| `in_cluster`            | auto-detect   | Force in-cluster vs out-of-cluster auth. |
| `startup_timeout`       | `60` seconds  | Wait for `Ready` in `start()`. |
| `idle_timeout`          | `3600`        | Passed through to `SessionManager`. |
| `labels`                | `{}`          | Extra labels merged into the pod metadata. |
| `env`                   | `{}`          | Extra container env vars. |
| `delete_on_stop`        | `True`        | Whether `stop()` deletes the pod. |
| `service_account_name`  | `"default"`   | Pod's service account; we recommend a dedicated no-privilege SA. |

## Lifecycle Management

```python
sandbox = KubernetesPodSandbox(image="...", namespace="agents")
sandbox.start()                       # creates the pod, waits for Ready
sandbox.is_alive()                    # True once Running + Ready

result = sandbox.execute("ls /")       # ExecuteResponse
content = sandbox.read("/etc/hostname")
sandbox.write("/workspace/note.md", "...")
sandbox.edit("/workspace/note.md", "draft", "v1")

sandbox.stop()                        # deletes the pod, idempotent
```

## Error Handling

```python
from pydantic_ai_backends import KubernetesPodSandbox

try:
    sandbox = KubernetesPodSandbox(image="...")
    sandbox.start()
except ImportError:
    # pip install pydantic-ai-backend[kubernetes]
    raise
except RuntimeError as e:
    # config load failed, pod create failed, or pod didn't reach Ready
    # within startup_timeout
    print(f"sandbox failed to start: {e}")
```

`execute()` swallows runtime errors into the `ExecuteResponse`, matching
`DockerSandbox` and `DaytonaSandbox`:

```python
result = sandbox.execute("nonexistent-cmd")
# ExecuteResponse(output="...", exit_code=127, truncated=False)
```

## Custom pod templates

For production, pass `pod_template=` with your full pod spec —
ResourceQuotas, NetworkPolicies, and a non-`default` ServiceAccount
should all be enforced at the namespace level, but the template lets
you control per-pod things (volumes, sidecars, runtime classes,
nodeSelectors).

```python
sandbox = KubernetesPodSandbox(
    image="ghcr.io/your-org/agent-sandbox:1.4",
    namespace="agents",
    pod_template={
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "ignored", "labels": {"team": "search"}},
        "spec": {
            "serviceAccountName": "agent-sandbox",
            "runtimeClassName": "gvisor",
            "containers": [{
                "name": "sandbox",
                "image": "ghcr.io/your-org/agent-sandbox:1.4",
                # the constructor merges env + image + ports
            }],
        },
    },
)
```

## Kubernetes vs Docker vs Daytona

| Feature                | Docker          | Daytona               | Kubernetes        |
| ---------------------- | --------------- | --------------------- | ----------------- |
| Where it runs          | local daemon    | managed cloud         | your cluster      |
| Multi-tenant isolation | weak (host)     | strong (provider VMs) | strong (NetPol + RBAC) |
| Warm pool              | not built-in    | not built-in          | not built-in (build it on top) |
| Startup latency        | ~500 ms         | seconds (cloud spin-up) | <2 s warm, ~5 s cold |
| Persistent workspace   | bind mount      | provider-managed      | PVC if you set one (default `emptyDir`) |
| Operational cost       | runs everywhere | per-sandbox cloud bill | your cluster |

## Next Steps

- [Multi-user with SessionManager](../examples/multi-user.md)
- [Kubernetes sandbox example](../examples/kubernetes-sandbox.md)
- [API reference](../api/kubernetes.md)
