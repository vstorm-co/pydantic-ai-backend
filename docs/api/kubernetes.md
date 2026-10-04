# `KubernetesPodSandbox`

The pod `KubernetesWorkspace` runs on, reached only through `pods/exec`. It runs
commands through the [`CommandRunner`][pydantic_ai_backends.protocol.CommandRunner]
protocol and has no file operations of its own — reach files through a workspace.

::: pydantic_ai_backends.backends.kubernetes.KubernetesPodSandbox
    options:
      show_source: false
      show_root_heading: true
      members:
        - __init__
        - id
        - pod_name
        - work_dir
        - start
        - attach
        - stop
        - is_alive
        - run_command
        - stop_command
