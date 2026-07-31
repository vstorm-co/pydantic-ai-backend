"""Tests for turning raw file bytes into readable text."""

import mimetypes
import types

import pytest

from pydantic_ai_backends import _text
from pydantic_ai_backends._text import (
    CHARDET_SAMPLE_BYTES,
    bytes_to_text,
    clean_pdf_text,
    decode_detected_text,
    decode_text,
    extract_pdf_text,
    is_text_extension,
)


class _RecordingChardet:
    """A chardet stand-in that records how much input it was handed."""

    def __init__(self, encoding: str = "utf-8", confidence: float = 0.99):
        self.encoding = encoding
        self.confidence = confidence
        self.seen_sizes: list[int] = []

    def detect(self, data: bytes) -> dict:
        self.seen_sizes.append(len(data))
        return {"encoding": self.encoding, "confidence": self.confidence}


@pytest.fixture
def chardet(monkeypatch: pytest.MonkeyPatch):
    """Install a recording chardet and hand it to the test."""
    recorder = _RecordingChardet()
    monkeypatch.setattr(_text, "load", lambda name, purpose: recorder)
    return recorder


class TestIsTextExtension:
    def test_known_text_and_code_extensions(self):
        assert is_text_extension("md")
        assert is_text_extension("py")

    def test_mimetype_detection_covers_unlisted_extensions(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(mimetypes.types_map, ".cfg", "text/x-config")
        assert is_text_extension("cfg")

    def test_json_mimetype_counts_as_text(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(mimetypes.types_map, ".jsonl", "application/json")
        assert is_text_extension("jsonl")

    def test_binary_extension_is_not_text(self):
        assert not is_text_extension("png")

    def test_unknown_extension_is_not_text(self):
        assert not is_text_extension("zzz")


class TestDecodeText:
    def test_detection_reads_only_a_prefix(self, chardet: _RecordingChardet):
        """chardet is linear in input size, so a whole large file costs seconds."""
        payload = b"a" * (CHARDET_SAMPLE_BYTES * 3)

        assert decode_text(payload) == payload.decode()
        assert chardet.seen_sizes == [CHARDET_SAMPLE_BYTES]

    def test_low_confidence_falls_through_to_the_common_encodings(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            _text, "load", lambda name, purpose: _RecordingChardet("utf-16", confidence=0.1)
        )
        assert decode_text("café".encode()) == "café"

    def test_undetectable_encoding_is_tried_first_then_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """An encoding Python does not know must not break the read."""
        monkeypatch.setattr(
            _text, "load", lambda name, purpose: _RecordingChardet("not-an-encoding")
        )
        assert decode_text(b"plain") == "plain"

    def test_replacement_characters_are_the_last_resort(
        self, monkeypatch: pytest.MonkeyPatch, chardet: _RecordingChardet
    ):
        chardet.encoding = None
        monkeypatch.setattr(_text, "FALLBACK_ENCODINGS", ("utf-8",))

        assert decode_text(b"\xff\xfe") == "��"


class TestDecodeDetectedText:
    def test_text_is_returned(self, chardet: _RecordingChardet):
        assert decode_detected_text(b"hello") == "hello"

    def test_detection_reads_only_a_prefix(self, chardet: _RecordingChardet):
        payload = b"a" * (CHARDET_SAMPLE_BYTES * 3)

        assert decode_detected_text(payload) == payload.decode()
        assert chardet.seen_sizes == [CHARDET_SAMPLE_BYTES]

    def test_binary_content_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            _text, "load", lambda name, purpose: _RecordingChardet("utf-8", confidence=0.5)
        )
        with pytest.raises(ValueError, match="Binary File"):
            decode_detected_text(bytes(bytearray(range(129, 255)) * 4))

    def test_undetected_encoding_still_tries_utf8(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(_text, "load", lambda name, purpose: _RecordingChardet(None))
        assert decode_detected_text(b"hello") == "hello"


class _Page:
    def __init__(self, text: str):
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _Reader:
    def __init__(self, pages: list[_Page], metadata: dict | None = None):
        self.pages = pages
        self.metadata = metadata


def _fake_pypdf(reader: _Reader) -> types.ModuleType:
    module = types.ModuleType("pypdf")
    module.PdfReader = lambda _stream: reader
    return module


class TestExtractPdfText:
    def _extract(self, monkeypatch: pytest.MonkeyPatch, reader: _Reader) -> str:
        monkeypatch.setattr(_text, "load", lambda name, purpose: _fake_pypdf(reader))
        return extract_pdf_text(b"%PDF-1.4")

    def test_pages_are_separated_and_numbered(self, monkeypatch: pytest.MonkeyPatch):
        text = self._extract(monkeypatch, _Reader([_Page("first"), _Page("second")]))

        assert "--- Page 1 ---" in text
        assert "--- Page 2 ---" in text
        assert "second" in text

    def test_metadata_is_prefixed_when_present(self, monkeypatch: pytest.MonkeyPatch):
        reader = _Reader(
            [_Page("body")],
            metadata={"/Title": "Report", "/Author": "Ada", "/Subject": "Q3"},
        )
        text = self._extract(monkeypatch, reader)

        assert text.startswith("Title: Report")
        assert "Author: Ada" in text
        assert "Subject: Q3" in text

    def test_metadata_without_known_fields_is_skipped(self, monkeypatch: pytest.MonkeyPatch):
        reader = _Reader([_Page("body")], metadata={"/Producer": "somebody"})
        assert self._extract(monkeypatch, reader).startswith("--- Page 1 ---")

    def test_blank_pages_are_dropped(self, monkeypatch: pytest.MonkeyPatch):
        text = self._extract(monkeypatch, _Reader([_Page("   "), _Page("real")]))

        assert "--- Page 2 ---" in text
        assert "--- Page 1 ---" not in text

    def test_pageless_pdf_is_an_error(self, monkeypatch: pytest.MonkeyPatch):
        with pytest.raises(ValueError, match="no pages"):
            self._extract(monkeypatch, _Reader([]))

    def test_pdf_without_extractable_text_is_an_error(self, monkeypatch: pytest.MonkeyPatch):
        with pytest.raises(ValueError, match="No extractable text"):
            self._extract(monkeypatch, _Reader([_Page("")]))

    def test_parse_failure_is_reported(self, monkeypatch: pytest.MonkeyPatch):
        module = types.ModuleType("pypdf")

        def explode(_stream):
            raise RuntimeError("corrupt")

        module.PdfReader = explode
        monkeypatch.setattr(_text, "load", lambda name, purpose: module)

        with pytest.raises(ValueError, match="Failed to parse PDF: corrupt"):
            extract_pdf_text(b"not a pdf")


class TestCleanPdfText:
    def test_collapses_whitespace_and_rejoins_hyphenation(self):
        cleaned = clean_pdf_text("hello   world \n next\n\n\n\nfar  \fpage\nhyphen-\nated")

        assert "hello world" in cleaned
        assert "\n\n\n" not in cleaned
        assert "hyphenated" in cleaned
        assert "\f" not in cleaned


class TestBytesToText:
    def test_text_extension_is_decoded(self, chardet: _RecordingChardet):
        assert bytes_to_text("md", b"# title") == "# title"

    def test_pdf_extension_is_extracted(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            _text, "load", lambda name, purpose: _fake_pypdf(_Reader([_Page("body")]))
        )
        assert "body" in bytes_to_text("pdf", b"%PDF-1.4")

    def test_unknown_extension_falls_back_to_detection(self, chardet: _RecordingChardet):
        assert bytes_to_text("zzz", b"plain") == "plain"
