"""Opt-in physical-input acceptance against an operator-selected disposable CDP service."""

from __future__ import annotations

import os

import pytest

from pydantic_ai_backends.browser import BrowserSession, WebSocketCDP

_ENDPOINT = os.environ.get("PYDANTIC_BACKENDS_TEST_CDP")


@pytest.mark.skipif(
    not _ENDPOINT, reason="Set PYDANTIC_BACKENDS_TEST_CDP for live browser acceptance"
)
async def test_real_browser_tabs_forms_selection_and_context_isolation() -> None:
    assert _ENDPOINT is not None
    transport = await WebSocketCDP.connect(_ENDPOINT)
    owner = await BrowserSession.create(transport)
    other = await BrowserSession.create(transport)
    try:
        tab = await owner.new_tab()
        foreign = await other.new_tab()
        await tab.evaluate(
            'document.body.innerHTML = `<form onsubmit="event.preventDefault();'
            'window.submitted=(window.submitted||0)+1"><input name="q"></form>`;'
            'document.querySelector("input").focus();'
        )
        await tab.type_text("abcdef")
        await tab.key("a", modifiers=2, commands=["selectAll"])
        selection = await tab.evaluate(
            'document.querySelector("input").selectionEnd-'
            'document.querySelector("input").selectionStart'
        )
        assert selection["result"] == {"type": "number", "value": 6, "description": "6"}
        await tab.key("Enter")
        submitted = await tab.evaluate("window.submitted||0")
        assert submitted["result"] == {"type": "number", "value": 1, "description": "1"}
        assert len(await tab.screenshot()) > 100
        assert foreign.id not in {page.id for page in await owner.tabs()}
        with pytest.raises(RuntimeError, match="does not belong"):
            await owner.tab(foreign.id)
        await owner.close_tab(tab.id)
        assert await owner.tabs() == []
    finally:
        await owner.aclose()
        await other.aclose()
        await transport.aclose()
