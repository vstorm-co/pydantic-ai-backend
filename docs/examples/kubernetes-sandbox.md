# Kubernetes Sandbox

Pods as sandboxes, for agents that run next to a cluster.

```bash
pip install "pydantic-ai-backend[console,kubernetes]"
```

The caller needs `pods` create/get/delete and `pods/exec` on the namespace:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: agent-sandboxes
  namespace: agents
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["create", "get", "delete"]
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create", "get"]
```

```python
import asyncio

from pydantic_ai import Agent

from pydantic_ai_backends import ConsoleCapability, KubernetesWorkspace

pods = KubernetesWorkspace(
    image="python:3.12-slim",
    namespace="agents",
    service_account_name="sandbox",  # one with no permissions of its own
)
agent = Agent("anthropic:claude-opus-5-5", capabilities=[pods, ConsoleCapability()])


async def main() -> None:
    result = await agent.run("Write a script that prints the first 20 primes and run it.")
    print(result.output)
    await pods.destroy(result.workspace.ref)


asyncio.run(main())
```

The default pod is non-root with a read-only root filesystem and `emptyDir` volumes at
`/workspace` and `/tmp`; see [Kubernetes](../concepts/kubernetes.md#the-pod) to replace it.
