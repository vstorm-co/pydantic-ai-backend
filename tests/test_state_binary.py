"""A `StateBackend` document has to survive a real JSON round trip.

The backend's use beyond one process is that a host persists `files` and hands
it back later. That worked for text and quietly did not for anything else:
binary was decoded with `errors="surrogateescape"` and stored as lines, which
round-trips exactly *in Python* — `json.dumps` emits the lone surrogates without
complaint and `json.loads` reads them back, so a Python-only test sees nothing
wrong. Encoding that JSON as UTF-8, which is what any driver does before it
reaches a database, raises; PostgreSQL `jsonb` rejects the unpaired escape
outright.

So the assertions here encode the document rather than only re-parsing it, and
`_dumps` is written the way a driver would write it. A test using the default
`ensure_ascii=True` passes against the old behaviour and proves nothing.
"""

from __future__ import annotations

import json

import pytest

from pydantic_ai_backends import StateBackend
from pydantic_ai_backends.types import FileData

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe"
"""Enough of a real PNG header to be undecodable as UTF-8 in two ways."""


def _dumps(backend: StateBackend) -> bytes:
    """The document as a driver would hand it to a database.

    `ensure_ascii=False` is the point: with the default the surrogates come out
    as ASCII escape sequences and encode happily, which is exactly how this
    stayed hidden.
    """
    return json.dumps(backend.files, ensure_ascii=False).encode("utf-8")


def _reload(backend: StateBackend) -> StateBackend:
    """The backend a host would get back after storing and loading its files."""
    return StateBackend(files=json.loads(_dumps(backend).decode("utf-8")))


class TestBinaryRoundTrip:
    def test_binary_content_survives_being_stored_as_json(self):
        backend = StateBackend()
        backend.write("/chart.png", PNG)

        assert _reload(backend).read_bytes("/chart.png") == PNG

    def test_a_document_holding_binary_encodes_as_utf8(self):
        backend = StateBackend()
        backend.write("/chart.png", PNG)

        # The assertion that fails on lone surrogates, and the one a database
        # driver makes on the caller's behalf.
        _dumps(backend)

    def test_read_bytes_returns_exactly_what_was_written(self):
        backend = StateBackend()
        backend.write("/chart.png", PNG)

        assert backend.read_bytes("/chart.png") == PNG

    def test_a_str_carrying_lone_surrogates_is_stored_as_bytes(self):
        """The one way text could still put an unserialisable value in.

        It is what a caller gets from decoding bytes with `surrogateescape`
        themselves, which is how the previous version handed content back.
        """
        backend = StateBackend()
        smuggled = PNG.decode("utf-8", errors="surrogateescape")

        backend.write("/smuggled.bin", smuggled)

        _dumps(backend)
        assert backend.read_bytes("/smuggled.bin") == PNG


class TestTextIsUnaffected:
    def test_bytes_that_decode_as_utf8_are_stored_as_text(self):
        """A script written as bytes stays readable, greppable and editable."""
        backend = StateBackend()
        backend.write("/app.py", b"print('hello')\nprint('again')")

        assert "encoding" not in backend.files["/app.py"]
        assert "print('hello')" in backend.read("/app.py")
        assert backend.grep_raw("again")
        assert backend.edit("/app.py", "hello", "world").error is None
        assert backend.read_bytes("/app.py") == b"print('world')\nprint('again')"

    def test_unicode_text_is_not_base64(self):
        backend = StateBackend()
        backend.write("/greeting.txt", "Hello 世界 🌍")

        assert "encoding" not in backend.files["/greeting.txt"]
        assert backend.read_bytes("/greeting.txt") == "Hello 世界 🌍".encode()
        _dumps(backend)


class TestBinaryIsNotTreatedAsText:
    def test_read_refuses_a_binary_file(self):
        backend = StateBackend()
        backend.write("/chart.png", PNG)

        result = backend.read("/chart.png")

        assert result.startswith("Error:")
        assert "binary" in result

    def test_edit_refuses_a_binary_file(self):
        backend = StateBackend()
        backend.write("/chart.png", PNG)

        result = backend.edit("/chart.png", "x", "y")

        assert result.error is not None
        assert "binary" in result.error

    def test_grep_skips_binary_files(self):
        """Matching a pattern against base64 would report a line nobody wrote."""
        backend = StateBackend()
        backend.write("/notes.txt", "IHDR is a PNG chunk")
        backend.write("/chart.png", PNG)

        matches = backend.grep_raw("IHDR")

        assert [m["path"] for m in matches] == ["/notes.txt"]

    @pytest.mark.parametrize("listing", ["ls", "glob"])
    def test_size_is_the_decoded_length(self, listing: str):
        """Not the length of the base64, which is a third larger."""
        backend = StateBackend()
        backend.write("/chart.png", PNG)

        entries = backend.ls_info("/") if listing == "ls" else backend.glob_info("*.png")

        assert [e["size"] for e in entries] == [len(PNG)]


class TestDocumentsThisBackendDidNotWrite:
    def test_a_document_from_before_encoding_existed_still_reads(self):
        """No marker means text, and its surrogates encode back to their bytes.

        A host that persisted a workspace under the previous version has such a
        document, and loading it must not lose the file it holds.
        """
        legacy: dict[str, FileData] = {
            "/chart.png": {
                "content": PNG.decode("utf-8", errors="surrogateescape").split("\n"),
                "created_at": "2026-01-01T00:00:00+00:00",
                "modified_at": "2026-01-01T00:00:00+00:00",
            }
        }

        assert StateBackend(files=legacy).read_bytes("/chart.png") == PNG

    def test_undecodable_base64_reads_as_empty_rather_than_an_error_string(self):
        """`read_bytes` never returns a message a caller could mistake for content."""
        corrupt: dict[str, FileData] = {
            "/chart.png": {
                "content": ["not base64 at all!"],
                "created_at": "2026-01-01T00:00:00+00:00",
                "modified_at": "2026-01-01T00:00:00+00:00",
                "encoding": "base64",
            }
        }

        assert StateBackend(files=corrupt).read_bytes("/chart.png") == b""

    def test_base64_split_across_lines_is_still_decoded(self):
        """A document a host reformatted, or one written by a future version."""
        import base64

        encoded = base64.b64encode(PNG).decode("ascii")
        split: dict[str, FileData] = {
            "/chart.png": {
                "content": [encoded[:4], encoded[4:]],
                "created_at": "2026-01-01T00:00:00+00:00",
                "modified_at": "2026-01-01T00:00:00+00:00",
                "encoding": "base64",
            }
        }

        assert StateBackend(files=split).read_bytes("/chart.png") == PNG
