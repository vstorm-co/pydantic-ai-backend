# Examples

| Example | What it shows |
|---------|--------------|
| [basic_capability.py](basic_capability.py) | ConsoleCapability in a Docker container |
| [readonly_agent.py](readonly_agent.py) | Read-only agent with READONLY_RULESET |
| [custom_permissions.py](custom_permissions.py) | Custom ruleset (read + execute, no write) |
| [multi_agent_permissions.py](multi_agent_permissions.py) | Two agents with different permissions in one workspace |
| [local_cli/](local_cli/) | An interactive terminal assistant |
| [predictive_analytics/](predictive_analytics/) | An analytics agent that writes and runs code in a container |
| [web_production/](web_production/) | A multi-user code sandbox API, one container per session |

## Running

```bash
export ANTHROPIC_API_KEY=your-key
uv run python examples/basic_capability.py
```
