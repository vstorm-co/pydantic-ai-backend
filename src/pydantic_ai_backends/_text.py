"""Turning raw file bytes into text an agent can read.

Sandboxes hand back bytes with no encoding metadata, so the encoding has to be
guessed, and a few common formats (PDF) need extracting rather than decoding.
"""

from __future__ import annotations

import mimetypes
import re
from io import BytesIO

from pydantic_ai_backends._optional import load

CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        "c",
        "cpp",
        "cs",
        "css",
        "go",
        "h",
        "html",
        "java",
        "js",
        "jsx",
        "php",
        "py",
        "rb",
        "rs",
        "sh",
        "sql",
        "ts",
        "tsx",
    }
)

TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {"csv", "json", "log", "md", "toml", "txt", "xml", "yaml", "yml"}
)

CHARDET_SAMPLE_BYTES = 32 * 1024
"""Prefix handed to chardet for detection.

chardet is pure Python and linear in input size, so scanning a whole
multi-megabyte file costs seconds of CPU while adding nothing over a sample.
"""

FALLBACK_ENCODINGS = ("utf-8", "utf-8-sig", "latin-1", "cp1252", "iso-8859-1")

CONFIDENT_DETECTION = 0.7
"""chardet confidence above which its verdict is used without a fallback pass."""


def is_text_extension(extension: str) -> bool:
    """Whether files with this extension hold plain text.

    Args:
        extension: Lowercase extension without the leading dot.
    """
    if extension in TEXT_EXTENSIONS or extension in CODE_EXTENSIONS:
        return True
    mime_type = mimetypes.types_map.get(f".{extension}")
    if mime_type is None:
        return False
    return mime_type.startswith("text") or "json" in mime_type


def bytes_to_text(extension: str, data: bytes) -> str:
    """Decode or extract `data` as text, dispatching on its extension.

    Raises:
        ValueError: If the content is binary or a PDF that cannot be parsed.
    """
    if is_text_extension(extension):
        return decode_text(data)
    if extension == "pdf":
        return extract_pdf_text(data)
    return decode_detected_text(data)


def decode_text(data: bytes) -> str:
    """Decode text of unknown encoding, never failing.

    Detection runs on a prefix and decoding on the whole payload. A confident
    verdict is tried first, then a fixed list of common encodings, and finally
    UTF-8 with replacement characters so a read always returns something.
    """
    detected, confidence = detect_encoding(data)

    encodings: list[str] = []
    if detected and confidence > CONFIDENT_DETECTION:
        encodings.append(detected)
    encodings.extend(FALLBACK_ENCODINGS)
    if detected and detected not in encodings:
        encodings.insert(0, detected)

    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, AttributeError, LookupError):
            continue

    return data.decode("utf-8", errors="replace")


def decode_detected_text(data: bytes) -> str:
    """Decode bytes of an unrecognised type, refusing binary content.

    Args:
        data: Raw file content.

    Raises:
        ValueError: If every candidate encoding yields mostly replacement
            characters, which means the file is not text at all.
    """
    detected, _ = detect_encoding(data)
    # Ordered, not a set, so the decode order is deterministic for borderline
    # inputs; utf-8 always gets a turn even when detection named it already.
    candidates = [detected, "utf-8"] if detected else ["utf-8"]

    seen: set[str] = set()
    for encoding in candidates:
        if encoding in seen:
            continue
        seen.add(encoding)
        text = data.decode(encoding, errors="replace")
        if text.count("\ufffd") < max(len(text) // 100, 2):
            return text

    raise ValueError("[Binary File]")


def detect_encoding(data: bytes) -> tuple[str | None, float]:
    """Guess the encoding of `data` from its first `CHARDET_SAMPLE_BYTES`."""
    chardet = load("chardet", purpose="encoding detection")
    detection = chardet.detect(data[:CHARDET_SAMPLE_BYTES])
    encoding: str | None = detection.get("encoding")
    confidence: float = detection.get("confidence", 0)
    return encoding, confidence


PDF_METADATA_FIELDS = (("/Title", "Title"), ("/Author", "Author"), ("/Subject", "Subject"))
"""PDF metadata keys worth giving the model as context, and their labels."""


def extract_pdf_text(data: bytes) -> str:
    """Extract a PDF's text, prefixed with whatever metadata it carries.

    Raises:
        ValueError: If the PDF cannot be parsed or holds no extractable text.
    """
    pypdf = load("pypdf", purpose="PDF reading")

    try:
        reader = pypdf.PdfReader(BytesIO(data))
        if len(reader.pages) == 0:
            raise ValueError("PDF contains no pages")

        parts: list[str] = []
        metadata = reader.metadata
        if metadata:
            for key, label in PDF_METADATA_FIELDS:
                if metadata.get(key):
                    parts.append(f"{label}: {metadata[key]}\n")
            parts.append("\n")

        for number, page in enumerate(reader.pages, 1):
            page_text = page.extract_text()
            if page_text and page_text.strip():
                parts.append(f"--- Page {number} ---\n")
                parts.append(clean_pdf_text(page_text))
                parts.append("\n\n")

        text = "".join(parts).strip()
        if not text:
            raise ValueError("No extractable text found in PDF")
        return text
    except Exception as e:
        raise ValueError(f"Failed to parse PDF: {e}") from e


def clean_pdf_text(text: str) -> str:
    """Strip the extraction artifacts that make PDF text hard for a model to read."""
    text = re.sub(r" +", " ", text)
    text = re.sub(r"\n ", "\n", text)
    text = re.sub(r" \n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Words split across a line break by hyphenation are rejoined.
    text = re.sub(r"(\w+)-\n(\w+)", r"\1\2", text)
    return text.replace("\f", "\n").strip()
