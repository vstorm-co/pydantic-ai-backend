# Backends API

## Async adaptation

::: pydantic_ai_backends.adapter.ensure_async
    options:
      show_root_heading: true

::: pydantic_ai_backends.adapter.is_async_backend
    options:
      show_root_heading: true

## Base classes

Subclass one of these to write your own sandbox: implement `execute` and `edit`
and every other file operation is derived from shell commands. See
[Writing your own backend](../concepts/backends.md#writing-your-own-backend) for
which one to pick.

::: pydantic_ai_backends.backends.base.BaseSandbox
    options:
      show_root_heading: true
      members:
        - __init__
        - id
        - last_activity
        - touch
        - start
        - is_alive
        - stop
        - execute
        - edit

::: pydantic_ai_backends.backends.base.AsyncBaseSandbox
    options:
      show_root_heading: true
      members:
        - __init__
        - id
        - last_activity
        - touch
        - start
        - is_alive
        - stop
        - execute
        - edit

## LocalBackend

::: pydantic_ai_backends.backends.local.LocalBackend
    options:
      show_root_heading: true
      members:
        - __init__
        - ls_info
        - read
        - write
        - edit
        - glob_info
        - grep_raw
        - execute
        - execute_enabled

## StateBackend

::: pydantic_ai_backends.backends.state.StateBackend
    options:
      show_root_heading: true
      members:
        - __init__
        - files
        - ls_info
        - read
        - write
        - edit
        - glob_info
        - grep_raw

## CompositeBackend

::: pydantic_ai_backends.backends.composite.CompositeBackend
    options:
      show_root_heading: true
      members:
        - __init__
        - ls_info
        - read
        - write
        - edit
        - glob_info
        - grep_raw
