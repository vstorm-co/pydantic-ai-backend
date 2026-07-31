# Remote API

Client and service for sandboxes that live in another process. See
[Remote Sandboxes](../concepts/remote.md) for the concepts and the security
model.

## RemoteSandbox

Needs the `remote` extra (`httpx`).

::: pydantic_ai_backends.remote.client.RemoteSandbox
    options:
      show_root_heading: true
      members:
        - __init__
        - session_id
        - start
        - stop
        - is_alive
        - resource_usage
        - execute
        - read
        - read_bytes
        - write
        - edit
        - exists
        - ls_info
        - glob_info
        - grep_raw

## WorkspaceArchive

Read-only view of the files sessions left behind, with no sandbox running. Needs
the `remote` extra.

::: pydantic_ai_backends.remote.client.WorkspaceArchive
    options:
      show_root_heading: true
      members:
        - __init__
        - ls
        - read
        - close

::: pydantic_ai_backends.remote.client.WorkspaceArchiveError
    options:
      show_root_heading: true

## SandboxdConfig

Everything the service decides on a client's behalf. Needs the `server` extra.

::: pydantic_ai_backends.remote.server.SandboxdConfig
    options:
      show_root_heading: true
      members:
        - resolve_runtime
        - limits_for

## SandboxRuntime

One entry in the service's allowlist: what an alias runs, and under which
ceilings.

::: pydantic_ai_backends.remote.server.SandboxRuntime
    options:
      show_root_heading: true
      members:
        - builds
        - resolved_runtime
        - describes
        - image_label

## create_app

::: pydantic_ai_backends.remote.server.create_app
    options:
      show_root_heading: true

## Wire protocol

The HTTP contract as Pydantic models — one source of truth for both sides.

::: pydantic_ai_backends.remote.wire
    options:
      show_root_heading: false
      members:
        - CreateSessionRequest
        - SessionCreated
        - SessionInfo
        - SessionList
        - SessionUsage
        - SessionEvent
        - SessionEvents
        - ServiceHealth
        - ServicePolicy
        - ServiceIndex
