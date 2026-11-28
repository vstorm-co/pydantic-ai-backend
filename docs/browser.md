# Browser environments

The browser backend owns private Chromium contexts and exact tabs. It does not
choose actions with a model or impose an application's authorization policy.
Install its optional transport dependencies:

```bash
pip install 'pydantic-ai-backend[browser]'
```

## Managed browser in sandboxd

This API is an unreleased preview; build and install a local wheel from this branch.
Configure an allowed `browser` runtime on the service and create its sandboxd
session first. The runtime must set `SANDBOXD_BROWSER_PROXY` to its operator-owned
HTTP proxy and join an isolated network that can reach only that proxy. Browser
startup refuses an absent proxy. The recipe alone is not a network firewall. Use the resulting session identity and session-scoped token below.
This client never creates a workspace: **Start** explicitly starts the browser in
that existing session; inspection and attachment never wake a missing container.

```python
import os

from pydantic_ai_backends.browser import SandboxdBrowser


async def work_in_browser() -> None:
    browser = SandboxdBrowser(
        base_url="https://sandboxd.example.com",
        session_id="browser-conversation-123",
        token=os.environ["SANDBOX_SESSION_TOKEN"],
    )
    await browser.start(idle_seconds=900, lifetime_seconds=3600)
    try:
        async with await browser.connect() as connection:
            context_id = connection.browser.context_id
            tab = await connection.browser.new_tab()
            await tab.navigate("https://example.com")
            tab_id = tab.id

        # The viewer's transport closed; the supervised context and DOM remain.
        async with await browser.connect(context_id=context_id) as connection:
            same_tab = await connection.browser.tab(tab_id)
            image = await same_tab.screenshot()
            print(f"Captured {len(image)} JPEG bytes")
    finally:
        # Explicit End retires the browser's dedicated runtime and live tabs.
        # Workspace file retention follows the service's storage policy.
        await browser.end()
```

Store `context_id` and tab identities in trusted application session state. They
are resource references, not authorization credentials. An explicit context or tab
that disappeared is refused; attachment never silently opens a substitute.

`ManagedBrowserConnection.aclose()` only detaches its CDP transport. It does not
end the supervised environment. `SandboxdBrowser.inspect()` reads current state
without extending idle or capturing screenshots. Deadline clock values are
monotonic service times; persistent profile metadata is described below.

The `websocket_url` and `connection_headers` properties support trusted server-side
CDP integrations. **Never expose the token or authenticated debugger connection to
an end-user browser UI.** CDP grants broad access to the authorized sandbox, like
its command API. Products must enforce their own viewer/input permissions before
using it.

Idle expiry and absolute lifetime can terminate a live browser. Stopping a
container may preserve workspace files, but does not promise restoration of
unsaved forms, DOM objects or JavaScript memory. Changing idle/lifetime policy
requires explicitly ending the existing environment first.

## Remembering sign-ins after sleep

Persistent profiles are an opt-in feature of the local preview. Set
`SANDBOXD_PERSIST_BROWSER_PROFILES=true` and `SANDBOXD_WORKSPACE_ROOT` on sandboxd,
then pass `profile_key` to `SandboxdBrowser.start()`. Generate it once with
`base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()` and store it in your
application's secret store. Reuse that key and the same tenant/session identity
when explicitly starting the environment again. Never send it to the viewer.
The server extra includes the encryption dependency.

This mode uses one Chromium default profile in a dedicated container. Cookies,
including session cookies supported by Chromium's restored profile, local storage
and IndexedDB survive a successful checkpoint. Clients sharing that session share
its browser storage and tabs. The synthetic context identity changes after a
restart: reconnect through the new state and discover tabs again. Old context/tab
references are refused rather than replaced silently.

`await browser.suspend()` checkpoints and hibernates the runtime. Persistent
`await browser.end()` also retains the encrypted profile; `await browser.purge()`
erases it and the session's retained files. Ephemeral End keeps its previous
retirement behavior. Passive inspection does not wake a sleeping environment;
call Start explicitly to restore it.

The profile archive is AES-256-GCM encrypted, bound to tenant and session, and
written atomically into the session's private storage. The profile key is neither
stored there nor passed into the container. Wrong keys and corrupt archives fail
closed. Plaintext transfer stays in process memory and the runtime's temporary
filesystem. Full profiles can contain credentials: authorize their reuse as
carefully as the application's vault. Archive size is capped at 64 MiB and 20,000
members; an oversized checkpoint fails visibly.

Normal sleep closes Chromium and waits for its processes to finish flushing
before saving. A periodic checkpoint runs at most once per 30 seconds, temporarily
stopping only Chromium's process group; the independent watchdog remains runnable.
A crash can lose changes since the last completed checkpoint. DOM objects,
unsaved forms, JavaScript memory and running processes are not restored.
`persistent`, `profile_restored`, `profile_saved_at` (UTC epoch seconds) and
`generation` in managed state describe the profile lifecycle; the other deadline
fields remain monotonic service times.

Applications choose whether a session belongs to a conversation, a user/agent or
a project. sandboxd enforces the supplied tenant/session binding; it does not
choose product scopes or consent. Do not attach a personal profile to a shared
conversation without an explicit authorization policy. To forget native sign-ins,
purge the profile and rotate its application-held key. Close only owned tabs when
one of several clients detaches; ending the shared environment affects them all.

A service restart loses live session tokens and registry entries, even though the
archive survives. Old scoped tokens cannot erase an orphan: trusted operator
credentials may purge it without creating a container. Reconcile orphan runtimes
before restoring retained state; this is not transparent daemon-crash recovery.

## Operator-managed CDP

For an existing vetted Chromium or hosted CDP endpoint, use the low-level API:

```python
from pydantic_ai_backends.browser import BrowserSession, WebSocketCDP


async def browse(endpoint: str) -> None:
    transport = await WebSocketCDP.connect(endpoint)
    session = await BrowserSession.create(transport)
    try:
        tab = await session.new_tab()
        await tab.navigate("https://example.com")
        await tab.key("Tab")
        await tab.type_text("Example")
        await tab.key("Enter")
    finally:
        await session.aclose()
        await transport.aclose()
```

Here the caller owns the context lifetime. Its creating transport requests
Chromium's `disposeOnDetach` protection, so an unexpected supervisor loss cannot
leave an abandoned context behind. The sandboxd gateway deliberately supervises
its contexts independently of viewers instead.

Tab methods provide navigation, screenshots, viewport fitting, click, wheel,
text and key input. Coordinates use CSS pixels. Key modifiers use CDP's bits:
Alt=1, Control=2, Meta=4, Shift=8. Chromium may require explicit editing commands
for shortcuts, particularly on macOS:

```python
await tab.key("a", modifiers=4, commands=["selectAll"])
```

`BrowserTab.aclose()` detaches that observer; `session.close_tab(tab.id)` closes
the actual page after checking context ownership. A shared transport remains
open when one session closes. Keep ownership explicit in application cleanup.

## Failure and network contracts

Commands have bounded deadlines and are never automatically replayed. A timeout
can mean that physical input already reached the page. Managed operations report
authorization refusal, policy conflict and unavailable resources separately.
Failed disposal remains retryable rather than being reported as completed.

Endpoints are trusted operator configuration. HTTP discovery retains the vetted
endpoint's authority rather than following a debugger host returned by a server.
Applications/operators must enforce endpoint allowlists and browser egress policy,
including redirects, subresources and DNS rebinding. A navigation URL check alone
is not network isolation. Private contexts isolate normal browser storage; use
separate managed containers where an independent process/resource boundary is
required.

## Operator lifecycle policy

`sandboxd` exposes `interactive_environments` in its authenticated policy response.
Startup settings `SANDBOXD_INTERACTIVE_IDLE_TIMEOUT` (900 seconds),
`SANDBOXD_INTERACTIVE_MAX_LIFETIME` (3600 seconds) and
`SANDBOXD_MAX_INTERACTIVE_ENVIRONMENTS` (100) bound interactive admission. Client
requests may ask for up to 86400 seconds; the service clamps them to its policy
and the workspace's idle timeout. These interactive settings require a service
restart; they are not writable through `PUT /policy`. Existing workspace policy
updates remain unchanged.

The sandboxd process owns live contexts while viewers attach and detach. A
sandboxd restart loses managed-access and resume metadata; a container restart
also loses unsaved live state. Neither provides transparent resumption.
Cleanup retries stay visible while this process is running. Dedicated managed
browser containers also carry an independent watchdog with an immutable hard
deadline: reconnecting cannot extend it, and supervisor loss cannot leave Chromium
running indefinitely. Normal idle enforcement still requires sandboxd.

Live registries and scoped session credentials are currently in memory. Restarting
sandboxd does not restore those identities or authorize old session tokens.
Applications must retain cleanup journals and operators must reconcile outstanding
resources after service outages; the watchdog bounds their remaining browser
lifetime, but does not restore them. This preview does not provide durable
interactive-session recovery.

## Live acceptance

The focused unit tests cover resource ownership and transport failures. To run the
opt-in physical-input acceptance against a disposable CDP service:

```bash
PYDANTIC_BACKENDS_TEST_CDP=http://127.0.0.1:9222 \
  uv run pytest tests/test_browser_integration.py -q
```

The test creates and disposes its own private contexts, checks real form submission,
text selection, screenshots and refusal to attach another context's tab. It does
not call a model or modify pre-existing user pages.
