"""Desktop adapters preserve workspace ownership and classify transport failures."""

import asyncio
import base64
import io
import json
import struct
from unittest.mock import AsyncMock, MagicMock

import anyio
import httpx
import pytest
from pydantic_ai.workspaces import CommandResult, Workspace

from pydantic_ai_backends.desktop import (
    Click,
    DaemonDesktop,
    DesktopError,
    DesktopUnavailableError,
    Launch,
    SandboxDesktop,
    TypeText,
    Wait,
    _sandbox,
    open_sandbox_desktop,
)
from pydantic_ai_backends.desktop._types import png_size
from pydantic_ai_backends.protocol import SandboxUnavailableError


def png(width=1024, height=768):
    return b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + struct.pack(">II", width, height)


@pytest.fixture
def workspace():
    box = MagicMock(spec=Workspace)
    box.attached = True
    box.backend = MagicMock()
    box.backend.run = AsyncMock()
    spilled = png() + b"x" * _sandbox._capture.INLINE_BYTES
    box.run = AsyncMock(
        return_value=CommandResult(
            exit_code=0, stdout=json.dumps({"mode": "spill", "size": len(spilled)}), stderr=""
        )
    )
    box.read_bytes = AsyncMock(return_value=spilled)
    box.remove = AsyncMock()
    return box


async def test_screenshot_uses_private_path_and_removes_it(workspace):
    desktop = SandboxDesktop(workspace)
    capture = await desktop.capture()
    assert (capture.width, capture.height) == (1024, 768)
    argv = workspace.run.call_args.args[0]
    assert argv[:2] == ["python3", "-c"]
    assert "scrot" in argv[2] and "--pointer" in argv[2]
    assert argv[3].startswith("/tmp/pydantic-desktop-screen-")
    workspace.remove.assert_awaited_once_with(argv[3])
    assert workspace.run.call_args.kwargs["env"] == {"DISPLAY": ":0"}
    await desktop.close()


@pytest.mark.parametrize("failure", [SandboxUnavailableError("gone"), OSError("gone")])
async def test_command_failure_is_unavailable_and_still_cleans(workspace, failure):
    workspace.run.side_effect = failure
    with pytest.raises(DesktopUnavailableError):
        await SandboxDesktop(workspace).capture()
    workspace.remove.assert_awaited_once()


async def test_read_failure_keeps_original_cause_and_cleans(workspace):
    workspace.read_bytes.side_effect = SandboxUnavailableError("gone")
    with pytest.raises(DesktopUnavailableError):
        await SandboxDesktop(workspace).capture()
    workspace.remove.assert_awaited_once()


async def test_cancelled_capture_cleans_without_swallowing_cancellation(workspace):
    workspace.read_bytes.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await SandboxDesktop(workspace).capture()
    workspace.remove.assert_awaited_once()


async def test_cancelled_capture_rpc_still_discards_possible_published_frame(workspace):
    workspace.run.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await SandboxDesktop(workspace).capture()
    workspace.read_bytes.assert_not_awaited()
    workspace.remove.assert_awaited_once()


async def test_cleanup_failure_is_visible_without_masking_capture(workspace, caplog):
    workspace.remove.side_effect = SandboxUnavailableError("sensitive")
    await SandboxDesktop(workspace).capture()
    assert "desktop_screenshot_cleanup_failed: SandboxUnavailableError" in caplog.text
    assert "sensitive" not in caplog.text


async def test_commands_and_text_stay_out_of_argv(workspace):
    desktop = SandboxDesktop(workspace)
    await desktop.perform(Click(x=1, y=2))
    await desktop.perform(TypeText(text="private'\ntext"))
    command = workspace.run.call_args
    assert "private" not in str(command.args)
    assert command.kwargs["env"]["PYDANTIC_DESKTOP_INPUT_TEXT"] == "private'\ntext"
    workspace.run.return_value = CommandResult(exit_code=1, stdout="private", stderr="private")
    with pytest.raises(DesktopError, match="exit 1") as error:
        await desktop.perform(TypeText(text="private"))
    assert "private" not in str(error.value)
    with pytest.raises(DesktopError, match="null"):
        await desktop.perform(TypeText(text="a\0b"))


async def test_wait_launch_and_apps(workspace):
    desktop = SandboxDesktop(workspace)
    await desktop.perform(Wait(seconds=0.001))
    workspace.run.assert_not_awaited()
    await desktop.perform(Launch(app="Firefox"))
    assert workspace.run.call_args.args[0][-2:] == ["launch", "Firefox"]
    workspace.run.return_value = CommandResult(
        exit_code=0, stdout='[{"id":"f","name":"Firefox","path":"/f"}]', stderr=""
    )
    assert (await desktop.apps())[0].id == "f"
    workspace.run.return_value = CommandResult(exit_code=0, stdout="bad", stderr="")
    with pytest.raises(DesktopError, match="applications"):
        await desktop.apps()


@pytest.mark.parametrize(
    "stdout,stderr,expected", [("", "", "exit 2"), ("why", "", "why"), ("", "why", "why")]
)
async def test_nonzero_commands_are_refused(workspace, stdout, stderr, expected):
    workspace.run.return_value = CommandResult(exit_code=2, stdout=stdout, stderr=stderr)
    with pytest.raises(DesktopError, match=expected):
        await SandboxDesktop(workspace).perform(Click(x=0, y=0))


async def test_open_reuses_workspace_and_reset_script_is_explicit(workspace):
    desktop = await open_sandbox_desktop(workspace, width=1024, height=768)
    assert isinstance(desktop, SandboxDesktop)
    script = workspace.run.call_args.args[0]
    assert script.startswith("xdpyinfo")
    assert "1024x768x24" in script
    assert workspace.run.call_args.kwargs == {"shell": True, "timeout": 90}
    await open_sandbox_desktop(workspace, width=800, height=600, reset_session=True)
    assert workspace.run.call_args.args[0].startswith("python3 -c")


async def test_open_requires_command_workspace():
    with pytest.raises(DesktopError, match="command-capable"):
        await open_sandbox_desktop(object(), width=800, height=600)


@pytest.mark.parametrize("code,expected", [(3, "no desktop"), (1, "did not start")])
async def test_open_failure(workspace, code, expected):
    workspace.run.return_value = CommandResult(exit_code=code, stdout="", stderr="")
    with pytest.raises(DesktopError, match=expected):
        await open_sandbox_desktop(workspace, width=800, height=600)


async def test_open_transport_failure_and_cancellation(workspace):
    workspace.run.side_effect = OSError("private")
    with pytest.raises(DesktopUnavailableError):
        await open_sandbox_desktop(workspace, width=800, height=600)
    workspace.run.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await open_sandbox_desktop(workspace, width=800, height=600)


@pytest.mark.parametrize("width,height", [(0, 1), (1, 0), (3841, 1), (1, 2161)])
def test_invalid_open_size(width, height):
    with pytest.raises(ValueError):
        _sandbox.desktop_start(width, height)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"<html>",
        png(0, 1),
        png(1, 0),
        png(32769, 1),
        png(1, 32769),
        b"\x89PNG\r\n\x1a\n" + b"x" * 16,
    ],
)
def test_invalid_png(data):
    with pytest.raises(DesktopError):
        png_size(data)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://host",
        "http://user:pass@host",
        "http://host?x",
        "http://host#x",
        "http://host:abc",
        "http://host:0",
    ],
)
def test_daemon_refuses_invalid_url(url):
    with pytest.raises(ValueError):
        DaemonDesktop(url, "private")


def test_daemon_requires_token():
    with pytest.raises(ValueError, match="empty"):
        DaemonDesktop("http://host", "")


async def test_daemon_capture_actions_apps_and_client_ownership():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/screenshot":
            return httpx.Response(
                200,
                content=png(200, 100),
                headers={"x-platform": "macOS", "x-screen-width": "100", "x-screen-height": "50"},
            )
        if request.url.path == "/apps":
            return httpx.Response(200, json=[{"id": "f", "name": "Firefox", "path": "/f"}])
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        desktop = DaemonDesktop("http://host/", "secret", client=client)
        capture = await desktop.capture()
        assert (capture.width, capture.height, desktop.label) == (100, 50, "macOS")
        await desktop.perform(Click(x=1, y=2))
        assert b'"action":"click"' in requests[-1].content
        assert (await desktop.apps())[0].id == "f"
        assert requests[0].headers["authorization"] == "Bearer secret"
        await desktop.close()
        assert not client.is_closed
    owned = DaemonDesktop("http://host", "secret")
    await owned.close()
    await owned.close()
    assert owned._client.is_closed


async def test_daemon_without_native_headers_uses_png_dimensions():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=png()))
    ) as client:
        capture = await DaemonDesktop("http://host", "secret", client=client).capture()
        assert (capture.width, capture.height) == (1024, 768)


@pytest.mark.parametrize(
    "headers",
    [
        {"x-screen-width": "bad"},
        {"x-screen-height": "0"},
        {"x-screen-width": "0"},
        {"x-screen-height": "40000"},
    ],
)
async def test_daemon_invalid_native_dimensions(headers):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=png(), headers=headers))
    ) as client:
        with pytest.raises(DesktopError, match="input dimensions"):
            await DaemonDesktop("http://host", "secret", client=client).capture()


@pytest.mark.parametrize(
    "status,error_type", [(401, DesktopError), (302, DesktopError), (500, DesktopUnavailableError)]
)
async def test_daemon_classifies_errors_without_echoing_body_or_redirecting(status, error_type):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            status, json={"error": "sensitive"}, headers={"location": "http://other"}
        )

    async with httpx.AsyncClient(
        follow_redirects=True, transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(error_type) as error:
            await DaemonDesktop("http://host", "secret", client=client).capture()
        assert "sensitive" not in str(error.value)
        assert len(requests) == 1


async def test_daemon_transport_failure_does_not_retry_unknown_action():
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        raise httpx.ConnectError("secret", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DesktopUnavailableError):
            await DaemonDesktop("http://host", "secret", client=client).perform(Click(x=1, y=2))
        assert count == 1


async def test_screenshot_cleanup_is_shielded_from_anyio_cancellation(workspace):
    async def cancelled_read(_path):
        await anyio.sleep_forever()

    workspace.read_bytes.side_effect = cancelled_read
    with anyio.CancelScope() as scope:
        scope.cancel()
        await SandboxDesktop(workspace).capture()
    workspace.remove.assert_awaited_once()


async def test_screenshot_cleanup_timeout_is_bounded_and_visible(workspace, monkeypatch, caplog):
    async def hung_remove(_path):
        await anyio.sleep_forever()

    workspace.remove.side_effect = hung_remove
    monkeypatch.setattr(_sandbox, "SCREENSHOT_CLEANUP_SECONDS", 0)
    capture = await SandboxDesktop(workspace).capture()
    assert capture.width == 1024
    assert "desktop_screenshot_cleanup_timed_out" in caplog.text


def test_standalone_desktop_helpers_have_no_app_imports():
    from pydantic_ai_backends.desktop import desktop_apps_script, desktop_session_script

    assert "def installed(" in desktop_apps_script()
    assert "retire(':0')" in desktop_session_script()
    assert "from app." not in desktop_apps_script() + desktop_session_script()


async def test_daemon_rejects_oversized_body_and_closes_stream(monkeypatch):
    from pydantic_ai_backends.desktop import _daemon

    monkeypatch.setattr(_daemon, "MAX_RESPONSE_BYTES", 2)
    closed = False

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"12"
            yield b"3"

        async def aclose(self):
            nonlocal closed
            closed = True

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Chunks()))
    ) as client:
        with pytest.raises(DesktopError, match="size limit"):
            await DaemonDesktop("http://host", "secret", client=client).capture()
        assert closed


async def test_daemon_cancellation_closes_response_stream():
    closed = False

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            raise asyncio.CancelledError

        async def aclose(self):
            nonlocal closed
            closed = True

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Chunks()))
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await DaemonDesktop("http://host", "secret", client=client).capture()
        assert closed


async def test_inline_screenshot_needs_exactly_one_rpc_and_no_file_operations(workspace):
    image = png()
    workspace.run.return_value = CommandResult(
        exit_code=0,
        stdout=json.dumps(
            {"mode": "inline", "size": len(image), "image_b64": base64.b64encode(image).decode()}
        ),
        stderr="",
    )
    captured = await SandboxDesktop(workspace).capture()
    assert captured.image == image
    workspace.run.assert_awaited_once()
    workspace.read_bytes.assert_not_awaited()
    workspace.remove.assert_not_awaited()


@pytest.mark.parametrize("stderr", ["", "secret-image-stderr"])
async def test_failed_capture_never_exposes_provider_output(workspace, stderr):
    workspace.run.return_value = CommandResult(
        exit_code=1, stdout="secret-image-stdout", stderr=stderr
    )
    with pytest.raises(DesktopError, match="exit 1") as error:
        await SandboxDesktop(workspace).capture()
    assert "secret-image" not in str(error.value)
    workspace.remove.assert_awaited_once()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"mode": "inline", "size": 1, "image_b64": 12},
        {"mode": "foreign", "size": 1},
        {"mode": "spill", "size": _sandbox._capture.INLINE_BYTES},
        {"mode": "spill", "size": _sandbox._capture.MAX_IMAGE_BYTES + 1},
    ],
)
async def test_invalid_capture_response_is_not_an_image_or_leaked_payload(workspace, body):
    workspace.run.return_value = CommandResult(exit_code=0, stdout=json.dumps(body), stderr="")
    with pytest.raises(DesktopError, match="invalid screenshot response") as error:
        await SandboxDesktop(workspace).capture()
    assert error.value.__suppress_context__
    workspace.remove.assert_awaited_once()


@pytest.mark.parametrize(
    "image_b64,size,reason",
    [
        ("$not-base64", 1, "invalid screenshot bytes"),
        (base64.b64encode(b"secret-image").decode(), 1, "size did not match"),
        (base64.b64encode(b"secret-image").decode(), len(b"secret-image"), "PNG screenshot"),
    ],
)
async def test_invalid_inline_bytes_fail_without_file_read_or_secret_error(
    workspace, image_b64, size, reason
):
    workspace.run.return_value = CommandResult(
        exit_code=0,
        stdout=json.dumps({"mode": "inline", "size": size, "image_b64": image_b64}),
        stderr="",
    )
    with pytest.raises(DesktopError, match=reason) as error:
        await SandboxDesktop(workspace).capture()
    assert "secret-image" not in str(error.value)
    workspace.read_bytes.assert_not_awaited()
    workspace.remove.assert_not_awaited()


async def test_spilled_capture_checks_exact_published_size_and_cleans(workspace):
    workspace.read_bytes.return_value = png()
    with pytest.raises(DesktopError, match="size did not match"):
        await SandboxDesktop(workspace).capture()
    workspace.remove.assert_awaited_once()


def capture_process(monkeypatch, *, data=b"", code=0, running=False, no_stdout=False):
    from pydantic_ai_backends.desktop import _capture

    process = MagicMock()
    process.stdout = None if no_stdout else io.BytesIO(data)
    process.wait.return_value = code
    process.poll.return_value = None if running else code
    monkeypatch.setattr(_capture.subprocess, "Popen", lambda *args, **kwargs: process)
    return process


def test_capture_script_streams_png_without_creating_tempfile(monkeypatch, tmp_path):
    from pydantic_ai_backends.desktop import _capture

    image = png()
    process = capture_process(monkeypatch, data=image)
    path = tmp_path / "screenshot.png"
    result = _capture.capture(str(path))
    assert result == {
        "mode": "inline",
        "size": len(image),
        "image_b64": base64.b64encode(image).decode(),
    }
    assert not path.exists() and process.stdout.closed
    process.kill.assert_not_called()


def test_capture_script_spills_the_same_frame_once_with_private_permissions(monkeypatch, tmp_path):
    from pydantic_ai_backends.desktop import _capture

    image = png() + b"x" * 100
    monkeypatch.setattr(_capture, "INLINE_BYTES", 16)
    monkeypatch.setattr(_capture, "CHUNK_BYTES", 8)
    process = capture_process(monkeypatch, data=image)
    path = tmp_path / "screenshot.png"
    assert _capture.capture(str(path)) == {"mode": "spill", "size": len(image)}
    assert path.read_bytes() == image and path.stat().st_mode & 0o777 == 0o600
    assert process.stdout.closed


@pytest.mark.parametrize("missing_output", [False, True])
def test_capture_script_rejects_failure_and_stops_its_process(
    monkeypatch, tmp_path, missing_output
):
    from pydantic_ai_backends.desktop import _capture

    process = capture_process(monkeypatch, code=2, running=True, no_stdout=missing_output)
    path = tmp_path / "screenshot.png"
    with pytest.raises(RuntimeError):
        _capture.capture(str(path))
    process.kill.assert_called_once()
    assert not path.exists()


def test_capture_script_caps_memory_and_cleans_partial_spill(monkeypatch, tmp_path):
    from pydantic_ai_backends.desktop import _capture

    monkeypatch.setattr(_capture, "INLINE_BYTES", 8)
    monkeypatch.setattr(_capture, "MAX_IMAGE_BYTES", 16)
    monkeypatch.setattr(_capture, "CHUNK_BYTES", 8)
    capture_process(monkeypatch, data=b"x" * 24)
    path = tmp_path / "screenshot.png"
    with pytest.raises(RuntimeError, match="size limit"):
        _capture.capture(str(path))
    assert not path.exists()


def test_capture_script_failure_keeps_existing_path_untouched(monkeypatch, tmp_path):
    from pydantic_ai_backends.desktop import _capture

    monkeypatch.setattr(_capture, "INLINE_BYTES", 1)
    capture_process(monkeypatch, data=png())
    path = tmp_path / "existing.png"
    path.write_bytes(b"existing work")
    with pytest.raises(FileExistsError):
        _capture.capture(str(path))
    assert path.read_bytes() == b"existing work"


def test_capture_script_main_emits_only_typed_reply(monkeypatch, capsys):
    from pydantic_ai_backends.desktop import _capture

    monkeypatch.setattr(_capture.sys, "argv", ["script", "/tmp/private-owned.png"])
    monkeypatch.setattr(_capture, "capture", lambda path: {"mode": "spill", "size": 1024})
    _capture.main()
    assert json.loads(capsys.readouterr().out) == {"mode": "spill", "size": 1024}
