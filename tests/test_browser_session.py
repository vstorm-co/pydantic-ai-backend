"""Private-context ownership and physical browser commands without a model."""

from __future__ import annotations

import base64
from collections.abc import Mapping

import pytest

from pydantic_ai_backends.browser import BrowserError, BrowserSession, BrowserTabInfo


class Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], str | None]] = []
        self.answers: dict[str, dict[str, object]] = {
            "Target.createBrowserContext": {"browserContextId": "own"},
            "Target.getBrowserContexts": {"browserContextIds": ["own"]},
            "Target.createTarget": {"targetId": "page"},
            "Target.getTargetInfo": {
                "targetInfo": {"targetId": "page", "browserContextId": "own", "type": "page"}
            },
            "Target.attachToTarget": {"sessionId": "physical"},
            "Target.getTargets": {
                "targetInfos": [
                    {"targetId": "page", "browserContextId": "own", "type": "page", "title": "Own"},
                    {"targetId": "secret", "browserContextId": "other", "type": "page"},
                    {"targetId": "worker", "browserContextId": "own", "type": "worker"},
                ]
            },
            "Page.captureScreenshot": {"data": base64.b64encode(b"jpeg").decode()},
        }
        self.failure: str | None = None
        self.closed = False

    async def call(
        self, method: str, params: Mapping[str, object], *, session_id: str | None = None
    ) -> dict[str, object]:
        self.calls.append((method, dict(params), session_id))
        if method == self.failure:
            raise BrowserError("Temporarily unavailable")
        return self.answers.get(method, {})

    async def aclose(self) -> None:
        self.closed = True


async def test_private_context_owned_until_explicit_dispose() -> None:
    transport = Transport()
    session = await BrowserSession.create(transport, proxy_server="http://egress:3128")
    assert transport.calls[0] == (
        "Target.createBrowserContext",
        {"disposeOnDetach": True, "proxyServer": "http://egress:3128"},
        None,
    )
    assert await session.tabs() == [BrowserTabInfo("page", "Own", "")]
    tab = await session.new_tab()
    assert tab.id == "page"
    assert transport.calls[-1] == (
        "Target.attachToTarget",
        {"targetId": "page", "flatten": True},
        None,
    )
    await tab.aclose()
    await tab.aclose()
    assert sum(method == "Target.detachFromTarget" for method, _, _ in transport.calls) == 1
    with pytest.raises(BrowserError, match="tab has ended"):
        await tab.reload()
    await session.aclose()
    await session.aclose()
    assert sum(method == "Target.disposeBrowserContext" for method, _, _ in transport.calls) == 1
    assert not transport.closed
    with pytest.raises(BrowserError, match="session has ended"):
        await session.tabs()


async def test_nonowning_attach_never_disposes_context() -> None:
    transport = Transport()
    session = BrowserSession(transport, "own", owns_context=False)
    await session.aclose()
    assert transport.calls == []
    with pytest.raises(ValueError, match="private context"):
        BrowserSession(transport, "")
    await BrowserSession.create(transport)
    assert transport.calls[-1][1] == {"disposeOnDetach": True}


@pytest.mark.parametrize("context,kind", [("other", "page"), ("own", "worker"), (None, "page")])
async def test_refuses_a_foreign_or_nonpage_target(context: str | None, kind: str) -> None:
    transport = Transport()
    transport.answers["Target.getTargetInfo"] = {
        "targetInfo": {"targetId": "page", "browserContextId": context, "type": kind}
    }
    with pytest.raises(BrowserError, match="does not belong"):
        await BrowserSession(transport, "own").tab("page")
    assert all(method != "Target.attachToTarget" for method, _, _ in transport.calls)


async def test_failed_disposal_preserves_cleanup_obligation() -> None:
    transport = Transport()
    session = BrowserSession(transport, "own")
    transport.failure = "Target.disposeBrowserContext"
    with pytest.raises(BrowserError):
        await session.aclose()
    transport.failure = None
    await session.aclose()
    assert sum(method == "Target.disposeBrowserContext" for method, _, _ in transport.calls) == 2


async def test_physical_commands_use_the_exact_attached_session() -> None:
    transport = Transport()
    tab = await BrowserSession(transport, "own").tab("page")
    await tab.navigate("https://example.com")
    assert await tab.screenshot() == b"jpeg"
    await tab.resize(640, 480, device_scale_factor=2)
    await tab.click(10, 20)
    await tab.type_text("a\ntext")
    await tab.key("Enter", code="Enter", modifiers=2)
    await tab.key("Tab")
    await tab.scroll(10, 20, delta_y=150)
    await tab.reload()
    assert await tab.evaluate("document.title") == {}
    assert all(session_id == "physical" for _, _, session_id in transport.calls[2:])
    assert ("Input.insertText", {"text": "a\ntext"}, "physical") in transport.calls
    transport.answers["Page.navigate"] = {"errorText": "secret destination"}
    with pytest.raises(BrowserError, match="could not navigate"):
        await tab.navigate("https://example.com")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "data:text/plain,x", "https://"])
async def test_navigation_refuses_local_and_invalid_schemes(url: str) -> None:
    transport = Transport()
    tab = await BrowserSession(transport, "own").tab("page")
    with pytest.raises(ValueError, match="HTTP"):
        await tab.navigate(url)
    assert all(method != "Page.navigate" for method, _, _ in transport.calls)


@pytest.mark.parametrize("quality,data", [(0, "ok"), (101, "ok"), (80, None), (80, "!!!")])
async def test_invalid_screenshot_inputs_and_replies(quality: int, data: object) -> None:
    transport = Transport()
    transport.answers["Page.captureScreenshot"] = {"data": data}
    tab = await BrowserSession(transport, "own").tab("page")
    with pytest.raises((ValueError, BrowserError)):
        await tab.screenshot(quality=quality)


@pytest.mark.parametrize(
    "width,height,scale",
    [(0, 480, 1.0), (640, 8193, 1.0), (640, 480, float("nan")), (640, 480, 5.0)],
)
async def test_viewport_bounds(width: int, height: int, scale: float) -> None:
    tab = await BrowserSession(Transport(), "own").tab("page")
    with pytest.raises(ValueError):
        await tab.resize(width, height, device_scale_factor=scale)


async def test_rejects_invalid_input_before_dispatch() -> None:
    tab = await BrowserSession(Transport(), "own").tab("page")
    for x, y in [(-1, 0), (0, float("inf"))]:
        with pytest.raises(ValueError, match="coordinates"):
            await tab.click(x, y)
    for text in ["\0", "a" * 65537]:
        with pytest.raises(ValueError, match="text"):
            await tab.type_text(text)
    with pytest.raises(ValueError, match="key"):
        await tab.key("")
    with pytest.raises(ValueError, match="Scroll"):
        await tab.scroll(0, 0, delta_y=float("inf"))
    with pytest.raises(ValueError, match="expressions"):
        await tab.evaluate("a" * 65537)


async def test_lost_cleanup_ack_and_exact_target_mismatch() -> None:
    transport = Transport()
    transport.answers["Target.getBrowserContexts"] = {"browserContextIds": []}
    await BrowserSession(transport, "own").aclose()
    assert all(method != "Target.disposeBrowserContext" for method, _, _ in transport.calls)
    transport.answers["Target.getTargetInfo"] = {
        "targetInfo": {"targetId": "different", "browserContextId": "own", "type": "page"}
    }
    session = BrowserSession(transport, "own")
    with pytest.raises(BrowserError, match="does not belong"):
        await session.tab("page")
    await session.close_tab("page")
    assert transport.calls[-1] == ("Target.closeTarget", {"targetId": "page"}, None)
    with pytest.raises(BrowserError, match="does not belong"):
        await session.close_tab("secret")


async def test_failed_new_tab_attachment_closes_its_new_target() -> None:
    transport = Transport()
    transport.failure = "Target.attachToTarget"
    with pytest.raises(BrowserError, match="Temporarily"):
        await BrowserSession(transport, "own").new_tab()
    assert transport.calls[-1] == ("Target.closeTarget", {"targetId": "page"}, None)


async def test_failed_new_tab_rollback_keeps_original_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingTransport(Transport):
        async def call(
            self, method: str, params: Mapping[str, object], *, session_id: str | None = None
        ) -> dict[str, object]:
            if method == "Target.closeTarget":
                raise OSError("cleanup failed")
            return await super().call(method, params, session_id=session_id)

    transport = FailingTransport()
    transport.failure = "Target.attachToTarget"
    with pytest.raises(BrowserError, match="Temporarily"):
        await BrowserSession(transport, "own").new_tab()
    assert "browser_tab_creation_cleanup_failed: OSError" in caplog.text


async def test_named_and_printable_keyboard_events_include_virtual_codes() -> None:
    transport = Transport()
    tab = await BrowserSession(transport, "own").tab("page")
    await tab.key("a", modifiers=4, commands=["selectAll"])
    assert transport.calls[-2] == (
        "Input.dispatchKeyEvent",
        {
            "key": "a",
            "code": "KeyA",
            "modifiers": 4,
            "windowsVirtualKeyCode": 65,
            "nativeVirtualKeyCode": 65,
            "type": "rawKeyDown",
            "commands": ["selectAll"],
        },
        "physical",
    )
    await tab.key("1")
    assert transport.calls[-2][1]["text"] == "1"
    with pytest.raises(ValueError, match="modifier"):
        await tab.key("Enter", modifiers=16)
    with pytest.raises(ValueError, match="button"):
        await tab.click(0, 0, button="extra")
    with pytest.raises(ValueError, match="editing"):
        await tab.key("a", commands=["arbitrary"])
