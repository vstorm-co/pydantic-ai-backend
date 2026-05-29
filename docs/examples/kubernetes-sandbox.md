# Kubernetes sandbox — minimal example

This walks through running `KubernetesPodSandbox` against a local
`kind` cluster. We'll build a pod image with the in-pod exec server,
push it into the cluster, and run a one-shot agent that uses it.

!!! tip "Already have a `DockerSandbox` image?"
    If you only need short-running commands, skip the HTTP server
    build entirely and use `mode="api"` — **any image you'd pass to
    `DockerSandbox` works as-is** (e.g. `python:3.12-slim`,
    `node:20-bookworm`, your own runtime image). The pod just needs
    `/bin/sh` and the caller needs `pods/exec` RBAC. Jump to
    [§5 — API mode with a plain image](#5-api-mode-with-a-plain-image).

## 1. Sandbox image with the HTTP exec server

The library ships the **client** but not the in-pod server (the server
choice is yours — Python, Go, anything that satisfies the route
contract). Here's a 30-line FastAPI server that does the minimum:

```python
# server.py
import asyncio, base64, hmac, os
from pathlib import Path
from fastapi import Depends, FastAPI, Header, HTTPException

OUTPUT_CAP = 100_000
TOKEN = os.environ["SANDBOX_EXEC_TOKEN"]


def auth(x_sandbox_token: str = Header(default="")) -> None:
    if not hmac.compare_digest(TOKEN, x_sandbox_token):
        raise HTTPException(status_code=401)


app = FastAPI()


@app.get("/health")
async def health(): return {"ready": True}


@app.post("/exec", dependencies=[Depends(auth)])
async def exec_(req: dict):
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", req["command"],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(
        proc.communicate(), timeout=req.get("timeout_seconds", 300)
    )
    return {
        "output": out[:OUTPUT_CAP].decode("utf-8", errors="replace"),
        "exit_code": proc.returncode,
        "truncated": len(out) > OUTPUT_CAP,
    }
```

```dockerfile
# Dockerfile
FROM python:3.13-slim
RUN pip install --no-cache-dir fastapi uvicorn
COPY server.py /app/server.py
WORKDIR /workspace
USER 1000:1000
EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", "--app-dir", "/app"]
```

## 2. Spin up `kind` and load the image

```sh
kind create cluster --name pab-demo
docker build -t pab-sandbox:demo .
kind load docker-image pab-sandbox:demo --name pab-demo

kubectl create namespace agents
kubectl create serviceaccount agent-sandbox --namespace agents
```

## 3. Run an agent

```python
import asyncio
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai_backends import KubernetesPodSandbox
from pydantic_ai_backends.toolsets.console import create_console_toolset


@dataclass
class Deps:
    sandbox: KubernetesPodSandbox


async def main() -> None:
    sandbox = KubernetesPodSandbox(
        image="pab-sandbox:demo",
        namespace="agents",
        sandbox_id="demo-1",
        service_account_name="agent-sandbox",
    )
    sandbox.start()
    try:
        agent = Agent[Deps, str](
            "openai:gpt-4o-mini",
            deps_type=Deps,
            toolsets=[create_console_toolset()],
        )
        result = await agent.run(
            "Create /workspace/hello.txt with 'hello world', then read it back",
            deps=Deps(sandbox=sandbox),
        )
        print(result.output)
    finally:
        sandbox.stop()


asyncio.run(main())
```

The pod is created on `start()`, the agent's tools call `execute()` /
`read()` / `write()` over HTTP through the `KubernetesPodSandbox`,
and the pod is deleted on `stop()`.

## 4. Use with SessionManager (multi-user)

```python
from pydantic_ai_backends import SessionManager, KubernetesPodSandbox


def make_sandbox(session_id: str) -> KubernetesPodSandbox:
    return KubernetesPodSandbox(
        image="pab-sandbox:demo",
        namespace="agents",
        sandbox_id=session_id,
        service_account_name="agent-sandbox",
    )


manager = SessionManager(sandbox_factory=make_sandbox)
sandbox = await manager.get_or_create("session-abc")
# ... use sandbox ...
manager.start_cleanup_loop(interval=60)
# on shutdown:
await manager.shutdown()
```

`SessionManager.cleanup_idle()` reads `sandbox._last_activity` and
calls `sandbox.stop()` (which deletes the pod). The factory plus the
manager give you per-session pod lifecycle without writing your own
controller.

For a production setup, layer NetworkPolicies, a ResourceQuota on the
sandbox namespace, and a janitor that periodically calls
`SessionManager.cleanup_idle()`. A built-in warm-pool factory is
proposed as a follow-up.

## 5. API mode with a plain image

If you don't want to build and maintain the HTTP exec server image,
`mode="api"` runs each command through the K8s `pods/exec`
subresource — the same mechanism `kubectl exec` uses. The image
contract collapses to "has a shell", so any image you'd hand to
`DockerSandbox` works untouched:

```python
from pydantic_ai_backends import KubernetesPodSandbox

sandbox = KubernetesPodSandbox(
    image="python:3.12-slim",   # same image you'd use with DockerSandbox
    namespace="agents",
    sandbox_id="demo-2",
    mode="api",
    service_account_name="agent-sandbox",
)
sandbox.start()
try:
    print(sandbox.execute("python -c 'print(1+1)'").output)  # "2"
finally:
    sandbox.stop()
```

The ServiceAccount needs `pods/exec` on the `agents` namespace:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: sandbox-exec, namespace: agents }
rules:
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: sandbox-exec, namespace: agents }
subjects:
  - kind: ServiceAccount
    name: agent-sandbox
    namespace: agents
roleRef:
  kind: Role
  name: sandbox-exec
  apiGroup: rbac.authorization.k8s.io
```

Trade-off: `pods/exec` can truncate sustained high-throughput output
(npm install, big test logs). For those workloads stay on
`mode="http"`. For short, deterministic commands, `mode="api"` is the
zero-extra-image path.
