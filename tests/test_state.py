"""`StateBackend`: a filesystem kept as a JSON document.

The document has to survive a real JSON round trip — the store's whole use beyond
one process is that a host persists `files` and `directories` and hands them back.
Binary used to be decoded with `errors="surrogateescape"` and stored as lines,
which round-trips *in Python* and fails the moment a driver encodes the JSON as
UTF-8; PostgreSQL `jsonb` rejects the unpaired escape outright. So the round trip
here encodes the document the way a driver would, with `ensure_ascii=False`.
"""

from __future__ import annotations

import base64
import json

import pytest

from pydantic_ai_backends import StateBackend
from pydantic_ai_backends.types import FileData

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe"
"""Enough of a real PNG header to be undecodable as UTF-8 in two ways."""


def _reload(state: StateBackend) -> StateBackend:
    """The store a host would get back after saving and loading its document."""
    saved = json.dumps(
        {"files": state.files, "directories": sorted(state.directories)}, ensure_ascii=False
    ).encode("utf-8")
    loaded = json.loads(saved.decode("utf-8"))
    return StateBackend(files=loaded["files"], directories=loaded["directories"])


def _legacy(content: list[str], **extra: str) -> dict[str, FileData]:
    entry: FileData = {
        "content": content,
        "created_at": "2026-01-01T00:00:00+00:00",
        "modified_at": "2026-01-01T00:00:00+00:00",
    }
    entry.update(extra)  # type: ignore[typeddict-item]
    return {"/chart.png": entry}


class TestRoundTrip:
    @pytest.mark.parametrize(
        "data", [PNG, b"text\n", b"in\n", b"", b"no newline", "zażółć\n".encode()]
    )
    def test_bytes_come_back_exactly_after_json(self, data: bytes) -> None:
        state = StateBackend()
        state.write_bytes("/f", data)
        assert _reload(state).read_bytes("/f") == data

    def test_text_is_stored_as_lines_and_binary_as_base64(self) -> None:
        state = StateBackend()
        state.write_bytes("/a.txt", b"one\ntwo")
        state.write_bytes("/b.png", PNG)
        assert state.files["/a.txt"]["content"] == ["one", "two"]
        assert "encoding" not in state.files["/a.txt"]
        assert state.files["/b.png"]["encoding"] == "base64"

    def test_a_rewrite_keeps_the_creation_time(self) -> None:
        state = StateBackend()
        state.write_bytes("/f", b"1")
        created = state.files["/f"]["created_at"]
        state.write_bytes("/f", b"2")
        assert state.files["/f"]["created_at"] == created

    def test_an_empty_directory_survives_a_round_trip(self) -> None:
        state = StateBackend()
        state.make_dir("/data/raw")
        reloaded = _reload(state)
        assert reloaded.is_dir("/data/raw") and reloaded.is_dir("/data")
        assert reloaded.list_dir("/data") == [("raw", True)]


class TestDocumentsThisClassDidNotWrite:
    def test_a_document_from_before_encoding_existed_still_reads(self) -> None:
        legacy = _legacy(PNG.decode("utf-8", errors="surrogateescape").split("\n"))
        assert StateBackend(files=legacy).read_bytes("/chart.png") == PNG

    def test_base64_split_across_lines_is_still_decoded(self) -> None:
        encoded = base64.b64encode(PNG).decode("ascii")
        split = _legacy([encoded[:4], encoded[4:]], encoding="base64")
        assert StateBackend(files=split).read_bytes("/chart.png") == PNG

    def test_undecodable_base64_is_an_error_rather_than_empty_content(self) -> None:
        corrupt = _legacy(["not base64 at all!"], encoding="base64")
        with pytest.raises(ValueError, match="does not decode"):
            StateBackend(files=corrupt).read_bytes("/chart.png")


class TestFilesystemRules:
    def test_paths_are_collapsed_as_text(self) -> None:
        state = StateBackend()
        state.write_bytes("a/./b/../c.txt", b"x")
        assert state.is_file("/a/c.txt")
        assert state.read_bytes("//a/c.txt") == b"x"

    def test_reading(self) -> None:
        state = StateBackend()
        state.write_bytes("/dir/file", b"x")
        with pytest.raises(IsADirectoryError):
            state.read_bytes("/dir")
        with pytest.raises(NotADirectoryError):
            state.read_bytes("/dir/file/child")
        with pytest.raises(FileNotFoundError):
            state.read_bytes("/missing")
        assert state.size("/dir/file") == 1

    def test_writing(self) -> None:
        state = StateBackend()
        state.write_bytes("/dir/file", b"x")
        with pytest.raises(IsADirectoryError):
            state.write_bytes("/dir", b"x")
        with pytest.raises(NotADirectoryError):
            state.write_bytes("/dir/file/child", b"x")

    def test_listing(self) -> None:
        state = StateBackend()
        state.write_bytes("/b.txt", b"x")
        state.write_bytes("/a/nested.txt", b"x")
        state.make_dir("/empty")
        assert state.list_dir("/") == [("a", True), ("b.txt", False), ("empty", True)]
        with pytest.raises(NotADirectoryError):
            state.list_dir("/b.txt")
        with pytest.raises(FileNotFoundError):
            state.list_dir("/nowhere")
        with pytest.raises(NotADirectoryError):
            state.list_dir("/b.txt/below")

    def test_making_directories(self) -> None:
        state = StateBackend()
        state.write_bytes("/file", b"x")
        state.make_dir("/")
        state.make_dir("/new")
        state.make_dir("/new")
        assert state.directories == {"/new"}
        with pytest.raises(FileExistsError):
            state.make_dir("/file")
        with pytest.raises(NotADirectoryError):
            state.make_dir("/file/child")

    def test_removing(self) -> None:
        state = StateBackend()
        state.write_bytes("/tree/a/file", b"x")
        state.make_dir("/tree/empty")
        state.write_bytes("/keep", b"x")
        state.remove("/keep")
        assert not state.exists("/keep")
        state.remove("/tree")
        assert state.files == {} and state.directories == set()
        with pytest.raises(FileNotFoundError):
            state.remove("/tree")

    def test_removing_everything_from_the_root(self) -> None:
        state = StateBackend(directories=["/d"])
        state.write_bytes("/f", b"x")
        state.remove("/")
        assert state.files == {} and state.directories == set()
