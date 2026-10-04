# Workspaces API

Pydantic AI workspaces backed by this library's sandboxes, and this library's
sandbox protocol over any workspace. See [Pydantic AI Workspaces](../concepts/workspaces.md)
for the concepts. Needs the `workspaces` extra.

## DockerWorkspace

::: pydantic_ai_backends.workspaces.DockerWorkspace
    options:
      show_root_heading: true
      members:
        - backend
        - get_workspace
        - destroy

::: pydantic_ai_backends.workspaces.DockerWorkspaceBackend
    options:
      show_root_heading: true
      members:
        - ref
        - working_dir
        - run

## SandboxdWorkspace

::: pydantic_ai_backends.workspaces.SandboxdWorkspace
    options:
      show_root_heading: true
      members:
        - backend
        - get_workspace
        - destroy

::: pydantic_ai_backends.workspaces.SandboxdWorkspaceBackend
    options:
      show_root_heading: true
      members:
        - ref
        - working_dir
        - run
        - purge

## WorkspaceSandbox

::: pydantic_ai_backends.workspaces.WorkspaceSandbox
    options:
      show_root_heading: true

## Commands under the workspace contract

::: pydantic_ai_backends.protocol.CommandRunner
    options:
      show_root_heading: true

::: pydantic_ai_backends.types.CommandOutcome
    options:
      show_root_heading: true

::: pydantic_ai_backends.protocol.SandboxUnavailableError
    options:
      show_root_heading: true
