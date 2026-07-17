# Permissions

The permission system provides fine-grained access control for file operations and shell commands. It uses pattern-based rules to allow, deny, or require approval for specific operations.

## Quick Start

```python
from pydantic_ai_backends import LocalBackend
from pydantic_ai_backends.permissions import DEFAULT_RULESET

# Use the default safe ruleset
backend = LocalBackend(
    root_dir="/workspace",
    permissions=DEFAULT_RULESET,
)

# Reads are allowed (except secrets)
content = backend.read("app.py")  # Works

# Writes require approval (in sync context, denied by default)
result = backend.write("output.txt", "data")  # Denied without callback
```

## Permission Actions

Each operation can result in one of three actions:

| Action | Behavior |
|--------|----------|
| `allow` | Operation proceeds immediately |
| `deny` | Operation is blocked with an error |
| `ask` | Requires user approval via callback |

## Pre-configured Presets

### DEFAULT_RULESET

Safe defaults for development environments:

- **Read**: Allowed, except for secrets (`.env`, `.pem`, `credentials`, etc.)
- **Write/Edit**: Requires approval
- **Execute**: Requires approval, dangerous commands blocked
- **Glob/Grep/Ls**: Allowed

```python
from pydantic_ai_backends.permissions import DEFAULT_RULESET

backend = LocalBackend(root_dir="/workspace", permissions=DEFAULT_RULESET)
```

### PERMISSIVE_RULESET

For trusted environments where most operations should succeed:

- **Read**: Allowed, except secrets
- **Write/Edit**: Allowed, except secrets and system files
- **Execute**: Allowed, dangerous commands blocked
- **Glob/Grep/Ls**: Allowed

```python
from pydantic_ai_backends.permissions import PERMISSIVE_RULESET

backend = LocalBackend(root_dir="/workspace", permissions=PERMISSIVE_RULESET)
```

### READONLY_RULESET

For read-only access:

- **Read**: Allowed, except secrets
- **Write/Edit/Execute**: Denied
- **Glob/Grep/Ls**: Allowed

```python
from pydantic_ai_backends.permissions import READONLY_RULESET

backend = LocalBackend(root_dir="/workspace", permissions=READONLY_RULESET)
```

### STRICT_RULESET

Everything requires explicit approval:

- **All operations**: Require approval
- **Secrets**: Denied

```python
from pydantic_ai_backends.permissions import STRICT_RULESET

backend = LocalBackend(root_dir="/workspace", permissions=STRICT_RULESET)
```

## Custom Rulesets

### Using create_ruleset()

Quick factory for common configurations:

```python
from pydantic_ai_backends.permissions import create_ruleset

# Allow reads and writes, but ask for execute
ruleset = create_ruleset(
    allow_read=True,
    allow_write=True,
    allow_execute=False,  # Will require approval
    deny_secrets=True,     # Block access to sensitive files
)
```

### Full Custom Configuration

For complete control, build a `PermissionRuleset`:

```python
from pydantic_ai_backends.permissions import (
    PermissionRuleset,
    OperationPermissions,
    PermissionRule,
)

custom_permissions = PermissionRuleset(
    default="deny",  # Global default for unconfigured operations

    read=OperationPermissions(
        default="allow",
        rules=[
            # Deny access to secrets
            PermissionRule(
                pattern="**/.env*",
                action="deny",
                description="Protect environment files",
            ),
            PermissionRule(
                pattern="**/secrets/**",
                action="deny",
                description="Protect secrets directory",
            ),
        ],
    ),

    write=OperationPermissions(
        default="ask",
        rules=[
            # Auto-allow Python files
            PermissionRule(pattern="**/*.py", action="allow"),
            # Auto-allow markdown
            PermissionRule(pattern="**/*.md", action="allow"),
        ],
    ),

    execute=OperationPermissions(
        default="deny",
        rules=[
            # Allow safe git commands
            PermissionRule(pattern="git status", action="allow"),
            PermissionRule(pattern="git diff*", action="allow"),
            PermissionRule(pattern="git log*", action="allow"),
            # Allow Python execution
            PermissionRule(pattern="python *", action="allow"),
            PermissionRule(pattern="pytest *", action="allow"),
        ],
    ),

    # Allow all search operations
    glob=OperationPermissions(default="allow"),
    grep=OperationPermissions(default="allow"),
    ls=OperationPermissions(default="allow"),
)
```

## Pattern Syntax

Patterns use glob-style matching:

| Pattern | Matches |
|---------|---------|
| `*` | Any characters except `/` |
| `**` | Any characters including `/` (recursive) |
| `?` | Any single character |
| `[abc]` | Any character in the set |
| `[!abc]` | Any character not in the set |

### Examples

```python
# Match .env files anywhere
PermissionRule(pattern="**/.env", action="deny")

# Match all .env* files (including .env.local, .env.production)
PermissionRule(pattern="**/.env*", action="deny")

# Match files in secrets directory
PermissionRule(pattern="**/secrets/**", action="deny")

# Match specific command
PermissionRule(pattern="rm -rf *", action="deny")

# Match command prefix
PermissionRule(pattern="git *", action="allow")
```

## Ask Callback

When a permission rule resolves to `"ask"`, the system invokes an `AskCallback` to determine whether the operation should proceed. The callback is an async function with the following signature:

```python
from pydantic_ai_backends.permissions import AskCallback

# Type: Callable[[PermissionOperation, str, str], Awaitable[bool]]
#                  operation         target reason
```

### CLI Approval

For interactive CLI applications, prompt the user directly:

```python
async def cli_approval(
    operation: str,  # "read", "write", "edit", "execute", etc.
    target: str,     # Path or command
    reason: str,     # Why approval is needed
) -> bool:
    response = input(f"Allow {operation} on '{target}'? [y/N] ")
    return response.lower() == "y"

backend = LocalBackend(
    root_dir="/workspace",
    permissions=DEFAULT_RULESET,
    ask_callback=cli_approval,
)
```

### Web App Approval

For web applications, you might store pending approvals and wait for a user response:

```python
import asyncio

# In-memory store for pending approvals
pending_approvals: dict[str, asyncio.Future[bool]] = {}

async def web_approval(operation: str, target: str, reason: str) -> bool:
    """Send approval request to frontend via WebSocket, wait for response."""
    approval_id = f"{operation}:{target}"
    future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    pending_approvals[approval_id] = future

    # Your WebSocket/SSE notification logic here
    await notify_frontend(operation, target, reason)

    try:
        # Wait up to 60 seconds for user to respond
        return await asyncio.wait_for(future, timeout=60.0)
    except asyncio.TimeoutError:
        return False  # Deny on timeout
    finally:
        pending_approvals.pop(approval_id, None)
```

### Auto-Approve with Logging

For trusted environments where you want to allow everything but keep an audit trail:

```python
import logging

logger = logging.getLogger("permissions")

async def auto_approve_with_logging(
    operation: str, target: str, reason: str
) -> bool:
    logger.info(f"Auto-approved: {operation} on '{target}' ({reason})")
    return True

backend = LocalBackend(
    root_dir="/workspace",
    permissions=STRICT_RULESET,  # Everything asks
    ask_callback=auto_approve_with_logging,
)
```

### Conditional Approval

Implement logic that auto-approves some operations and denies others:

```python
# Auto-approve writes to temp directories, deny everything else
async def conditional_approval(
    operation: str, target: str, reason: str
) -> bool:
    if operation == "write" and target.startswith("/temp/"):
        return True
    if operation == "execute" and target.startswith("python "):
        return True
    return False
```

## Ask Fallback

When a permission rule resolves to `"ask"` but no `ask_callback` is provided (or the callback returns `False`), the `AskFallback` setting controls what happens.

There are two fallback modes:

| Fallback | Behavior |
|----------|----------|
| `"deny"` | Raises `PermissionDeniedError` (operation silently blocked) |
| `"error"` | Raises `PermissionError` (signals that approval was needed but unavailable) |

```python
from pydantic_ai_backends.permissions import AskFallback

# Deny operations that need approval (safe for automated pipelines)
backend = LocalBackend(
    root_dir="/workspace",
    permissions=DEFAULT_RULESET,
    ask_fallback="deny",
)

# Raise PermissionError - useful for detecting missing callback setup
backend = LocalBackend(
    root_dir="/workspace",
    permissions=DEFAULT_RULESET,
    ask_fallback="error",
)
```

### Using PermissionChecker Directly

For programmatic use outside of backends, use `PermissionChecker` with explicit callback and fallback:

```python
from pydantic_ai_backends.permissions import PermissionChecker, DEFAULT_RULESET

async def my_callback(op: str, target: str, reason: str) -> bool:
    return input(f"Allow {op} on {target}? ").lower() == "y"

checker = PermissionChecker(
    ruleset=DEFAULT_RULESET,
    ask_callback=my_callback,
    ask_fallback="error",  # Raise if callback unavailable
)

# Async check - invokes callback for "ask" actions
try:
    allowed = await checker.check("write", "/config/settings.yaml", "Save settings")
except PermissionError:
    print("No callback available to ask for permission")
except PermissionDeniedError:
    print("Permission explicitly denied")

# Sync check - returns action without invoking callback
action = checker.check_sync("write", "/config/settings.yaml")
if action == "ask":
    print("This operation would need approval")
```

## Integration with LocalBackend

Permissions are checked after `allowed_directories`:

```python
backend = LocalBackend(
    root_dir="/workspace",
    allowed_directories=["/workspace", "/data"],  # Checked first
    permissions=DEFAULT_RULESET,                   # Checked second
)

# Path must pass BOTH checks:
# 1. Is the path within allowed_directories?
# 2. Does the permission ruleset allow this operation?
```

### How each operation applies the rules

- **`read` / `read_bytes`** — full "read" semantics: `deny` blocks, `ask`
  follows the callback / `ask_fallback`. `read_bytes` returns `b""` on a
  denied path (its documented "empty bytes on failure" contract).
- **`write` / `edit`** — full "write" / "edit" semantics.
- **`ls_info` / `glob_info`** — hide entries (and whole listings) with an
  explicit `deny` rule for the "ls" / "glob" operation. Because a listing
  can't prompt for approval, `ask` is treated as visible here — only `deny`
  hides.
- **`grep_raw`** — an explicit "grep" `deny` on the search path errors the
  search; individual files denied for "grep" **or for "read"** never
  contribute matches, so read-protected content can't leak through search
  results.
- **`execute`** — see below.

### Shell execution and permission rules

Execute rules pattern-match the **command string**, not file paths — a rule
like `PermissionRule(pattern="rm *", action="deny")` blocks `rm` commands,
but a read deny on `**/restricted/**` says nothing about `cat restricted/x`.

As defense-in-depth, `LocalBackend` also resolves path-looking tokens in the
command and denies it when one hits a "read" or "write" deny rule, so the
straightforward bypass (`cat restricted/secret.txt`) is caught.

**This guard is a speed bump, not a security boundary.** A shell can always
reach a file in ways command-string inspection cannot see (subshells,
`python -c`, encodings, …). If you need enforced path isolation *and* shell
access, run commands in a sandboxed backend such as `DockerSandbox` (mount
only what the agent may touch), or set the execute default to `"deny"` or
`"ask"`:

```python
ruleset = PermissionRuleset(
    read=OperationPermissions(
        default="allow",
        rules=[PermissionRule(pattern="**/restricted/**", action="deny")],
    ),
    execute=OperationPermissions(default="deny"),  # shell can't bypass file rules
)
```

## Integration with Console Toolset

Pass permissions to the toolset:

```python
from pydantic_ai_backends import create_console_toolset
from pydantic_ai_backends.permissions import DEFAULT_RULESET

# Toolset uses permissions to determine approval requirements
toolset = create_console_toolset(permissions=DEFAULT_RULESET)
```

When `permissions` is provided, the legacy `require_write_approval` and `require_execute_approval` flags are ignored.

## PermissionChecker

For programmatic permission checking:

```python
from pydantic_ai_backends.permissions import PermissionChecker, DEFAULT_RULESET

checker = PermissionChecker(ruleset=DEFAULT_RULESET)

# Synchronous check (returns action without asking)
action = checker.check_sync("read", "/path/to/file.txt")
# Returns: "allow", "deny", or "ask"

# Convenience methods
if checker.is_allowed("read", "/path/to/file.txt"):
    print("Read is allowed")

if checker.is_denied("write", "/etc/passwd"):
    print("Write is denied")

if checker.requires_approval("execute", "rm -rf /tmp/*"):
    print("Execute requires approval")
```

## Built-in Patterns

The presets use these common patterns:

### SECRETS_PATTERNS

```python
SECRETS_PATTERNS = [
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.crt",
    "**/credentials*",
    "**/secrets*",
    "**/*secret*",
    "**/*password*",
    "**/.aws/**",
    "**/.ssh/**",
    "**/.gnupg/**",
]
```

### SYSTEM_PATTERNS

```python
SYSTEM_PATTERNS = [
    "/etc/**",
    "/var/**",
    "/usr/**",
    "/bin/**",
    "/sbin/**",
    "/boot/**",
    "/sys/**",
    "/proc/**",
]
```

## Next Steps

- [Backends](backends.md) - Backend configuration
- [Console Toolset](console-toolset.md) - Tool configuration
- [API Reference](../api/permissions.md) - Complete API
