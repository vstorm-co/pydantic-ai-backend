"""What each extra installs: an extra missing a module its code loads fails only when used."""

from __future__ import annotations

import re
from importlib.metadata import requires


def _extra(name: str) -> set[str]:
    """The distributions the installed package's `name` extra requires."""
    marker = re.compile(rf"""extra\s*==\s*['"]{re.escape(name)}['"]""")
    return {
        re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0].lower()
        for requirement in requires("pydantic-ai-backend") or []
        if marker.search(requirement)
    }


def test_the_console_extra_installs_what_reading_a_text_file_needs() -> None:
    """`read_file` detects every text file's encoding with chardet, which came only
    with `docker` - so an install of `console` alone answered every read with
    "chardet is required"."""
    assert "chardet" in _extra("console")
