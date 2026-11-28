"""Check each interactive extra in an environment containing only that extra."""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("extra", choices=("browser", "desktop", "server"))
    extra = parser.parse_args().extra
    if extra == "browser":
        from pydantic_ai_backends.browser import BrowserSession, SandboxdBrowser

        assert BrowserSession is not None
        assert SandboxdBrowser("http://localhost", "session", "fixture-token").websocket_url
    elif extra == "desktop":
        from pydantic_ai_backends.desktop import Key, SandboxDesktop

        assert SandboxDesktop is not None
        assert Key(keys="enter").keys == "enter"
    else:
        from pydantic_ai_backends.remote.server import SandboxdConfig, create_app

        app = create_app(
            SandboxdConfig(token="fixture-token", prewarm=False),
            sandbox_builder=lambda *_args: None,
        )
        assert app.title == "sandboxd"


if __name__ == "__main__":
    main()
