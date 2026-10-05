# Examples

Each example is a complete agent you can run, built from a workspace and the console tools.

| Example | Workspace | Shows |
|---|---|---|
| [Local Workspace](local-workspace.md) | Pydantic AI's `LocalWorkspace` | A coding assistant on your own files |
| [Docker Sandbox](docker-sandbox.md) | `DockerWorkspace` | Model-written code in a container, continued across turns |
| [Kubernetes Sandbox](kubernetes-sandbox.md) | `KubernetesWorkspace` | Pods as sandboxes |
| [Remote Sandbox](remote-sandbox.md) | `SandboxdWorkspace` | An app that never holds the Docker socket |
| [Multi-User App](multi-user.md) | `SandboxdWorkspace` | One sandbox per conversation in a web app |
| [CLI Agent](cli-agent.md) | `LocalWorkspace` + permissions | An interactive terminal assistant that asks before it acts |

All of them need the console tools and a model:

```bash
pip install "pydantic-ai-backend[console]"
export ANTHROPIC_API_KEY=...
```
