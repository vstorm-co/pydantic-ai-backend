# Desktop environments

Install `pydantic-ai-backend[desktop]`. Native desktop operations require no model or agent. They work over the workspace an application already owns; they do not create a second computer.

```python
from pydantic_ai_backends.desktop import Key, TypeText, open_sandbox_desktop

# workspace is an existing Pydantic AI Workspace backed by sandboxd or a provider.
desktop = await open_sandbox_desktop(workspace, width=1280, height=800)
try:
    apps = await desktop.apps()
    screenshot = await desktop.capture()
    await desktop.perform(TypeText(text="hello"))
    await desktop.perform(Key(keys="enter"))
finally:
    await desktop.aclose()
```

The selected runtime needs Xvfb, XFCE, scrot and xdotool. The built-in `desktop` runtime installs these with Firefox. Applications choose a container's tenant, CPU/RAM/PID limits, network and idle policy through their workspace provider. `DaemonDesktop` instead attaches to an operator-owned desktop with an explicit URL/token. It never follows redirects or shuts down that machine. Authorize daemon destinations before constructing it.

With `SandboxdWorkspace`, the daemon and Desktop container may run on a separate
host from AgenticOS or the application backend. Configure the daemon endpoint and
vault-held credential in that backend; screen, input and file operations use the
remote workspace API, without mounting Docker into the client application. Use
TLS or a protected tunnel for the remote control connection. The daemon's
`workspace_root` and persistent HOME are paths on that daemon host.

Keep a retained session pinned to its assigned node. Additional nodes need stable
tenant/session placement and separate storage ownership; a random load balancer
or active-active shared root cannot restore a Desktop's state. See
[remote placement and ownership](concepts/remote.md#remote-hosts-and-placement).

Closing a desktop adapter detaches it; sandboxd/provider owns the container and workspace files. Restarting an application client can attach to the same exact live workspace, preserving open windows. Stopping or hibernating its container does not promise preservation of unsaved GUI state. A retained workspace ref must not silently stand in for a fresh desktop when the original is lost.

`DesktopWorkspace` is the small consumer contract for native screen operations, compatible with Pydantic AI Workspace. An application adapter translates provider-specific workspace failures into `SandboxUnavailableError`. Screenshots up to 512 KiB return in one workspace command without a temporary file. Larger screenshots transfer the same captured frame through a private temporary file with bounded shielded cleanup, including cancellation. Captures are capped at 32 MiB. `Capture` reports source dimensions; `to_native` maps a selected screenshot coordinate back to that source.

For a manual sandboxd environment, explicitly start `POST /sessions/{session_id}/environments/desktop` with the session token. Its product-level idle/hard deadlines are independent of workspace storage retention. A Desktop can share a workspace with console tools; ending its manual attachment does not delete shared work or stop its GUI processes. Its deadline fences the attachment; the workspace provider still owns retirement of the shared container. Do not expose session tokens or raw workspace execution to a browser viewer.

## Retaining files and settings

For the local preview, set `SANDBOXD_PERSIST_DESKTOP_HOME=true` together with
`SANDBOXD_WORKSPACE_ROOT`. sandboxd stores Desktop HOME separately from the public
workspace directory. Files in `/workspace` and settings under HOME, including
`.config`, survive idle retirement and container replacement. Both follow the
session's existing retention and purge policy. HOME is not included in public
workspace archive/file endpoints.

The built-in runtime uses `/root` for root and `/home/agent` for a non-root user.
Custom desktop runtimes must declare `SandboxRuntime.home_path` explicitly: `/root`
or a directory beneath `/home`, without traversal or overlapping workspace mounts.
HOME programs and configuration can survive replacement; system-wide `apt`
changes require retained containers or an updated image. Unsaved windows, RAM and
running processes are not snapshots and must be reopened after sleep.

Enabling this setting does not add a HOME mount to containers that already exist.
Before recreating an affected retained container, preserve its existing HOME and
migrate those files into the configured private HOME storage. Then explicitly
recreate the container with the new mount and verify its files and settings.
Restarting sandboxd alone reattaches the old container and does not change its
mounts. Keep the old data until the migration has been verified.

Keep the same authorized tenant/session identity to resume retained files.
Applications decide conversation/user/project scope, just as for sandbox
workspaces. Personal HOME state must not be reused across unrelated people.

Persistent Browser profiles and Desktop HOME require a single sandboxd owner for
that storage root. The service holds a lifetime filesystem lock and refuses a
second persistent owner before provisioning. Stop the old process before starting
its replacement; use a Recreate deployment strategy. Older releases do not
participate in this lock. Filesystem locking and storage permissions must work on
the chosen volume; these settings do not provide active-active ownership.
