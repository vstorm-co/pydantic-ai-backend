# Workspaces API

Pydantic AI workspace capabilities backed by this library's sandboxes. See
[Workspaces](../concepts/workspaces.md) for the concepts. Needs the `workspaces`
extra, plus the provider's own (`docker`, `kubernetes`, `daytona`).

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

## KubernetesWorkspace

::: pydantic_ai_backends.workspaces.KubernetesWorkspace
    options:
      show_root_heading: true
      members:
        - backend
        - get_workspace
        - destroy

::: pydantic_ai_backends.workspaces.KubernetesWorkspaceBackend
    options:
      show_root_heading: true
      members:
        - ref
        - working_dir
        - run

## DaytonaWorkspace

::: pydantic_ai_backends.workspaces.DaytonaWorkspace
    options:
      show_root_heading: true
      members:
        - backend
        - get_workspace
        - destroy

::: pydantic_ai_backends.workspaces.DaytonaWorkspaceBackend
    options:
      show_root_heading: true
      members:
        - ref
        - working_dir
        - run
        - purge

## StateWorkspace

::: pydantic_ai_backends.workspaces.StateWorkspace
    options:
      show_root_heading: true
      members:
        - backend
        - get_workspace
        - destroy

::: pydantic_ai_backends.workspaces.StateWorkspaceBackend
    options:
      show_root_heading: true
      members:
        - ref
        - working_dir
        - read_bytes
        - write_bytes
        - stat
        - list_dir
        - make_dir
        - remove
        - exists

## Commands under the workspace contract

::: pydantic_ai_backends.types.CommandOutcome
    options:
      show_root_heading: true
