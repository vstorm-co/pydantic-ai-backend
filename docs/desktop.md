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

Closing a desktop adapter detaches it; sandboxd/provider owns the container and workspace files. Restarting an application client can attach to the same exact live workspace, preserving open windows. Stopping or hibernating its container does not promise preservation of unsaved GUI state. A retained workspace ref must not silently stand in for a fresh desktop when the original is lost.

`DesktopWorkspace` is the small consumer contract for native screen operations, compatible with Pydantic AI Workspace. An application adapter translates provider-specific workspace failures into `SandboxUnavailableError`. Screenshots up to 512 KiB return in one workspace command without a temporary file. Larger screenshots transfer the same captured frame through a private temporary file with bounded shielded cleanup, including cancellation. Captures are capped at 32 MiB. `Capture` reports source dimensions; `to_native` maps a selected screenshot coordinate back to that source.

For a manual sandboxd environment, explicitly start `POST /sessions/{session_id}/environments/desktop` with the session token. Its product-level idle/hard deadlines are independent of workspace storage retention. A Desktop can share a workspace with console tools; ending its manual attachment does not delete shared work or stop its GUI processes. Its deadline fences the attachment; the workspace provider still owns retirement of the shared container. Do not expose session tokens or raw workspace execution to a browser viewer.
