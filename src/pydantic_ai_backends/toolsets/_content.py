"""Returning images and documents to the model as binary content.

Reading a PNG or a PDF as text gives a model nothing useful. Multimodal models
accept the raw bytes instead, so `read_file` hands those kinds back as
`BinaryContent` rather than decoding them.
"""

from __future__ import annotations

import io

from pydantic_ai import BinaryContent

from pydantic_ai_backends._optional import load_optional
from pydantic_ai_backends.adapter import ensure_async
from pydantic_ai_backends.protocol import AsyncBackendProtocol, BackendProtocol

IMAGE_EXTENSIONS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
"""Extensions recognized as images when image_support is enabled."""

IMAGE_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
"""Extension to MIME media type for images."""

DEFAULT_MAX_IMAGE_BYTES: int = 50 * 1024 * 1024
"""Default maximum image file size (50MB)."""

DEFAULT_MAX_IMAGE_DIMENSION: int = 1568
"""Longest image edge (px) sent to the model.

Matches Anthropic's recommended size, so large screenshots stop wasting tokens.
Requires Pillow (the `images` extra) and is a no-op without it.
"""

DOCUMENT_EXTENSIONS: frozenset[str] = frozenset({"pdf"})
"""Extensions recognized as binary documents when document_support is enabled.

Deliberately separate from `IMAGE_EXTENSIONS`: images and documents are distinct
kinds that may come to be handled differently — OCR for images, native document
understanding or text extraction for PDF and DOCX.
"""

DOCUMENT_MEDIA_TYPES: dict[str, str] = {"pdf": "application/pdf"}
"""Extension to MIME media type for documents."""

DEFAULT_MAX_DOCUMENT_BYTES: int = 50 * 1024 * 1024
"""Default maximum document file size (50MB)."""

BYTES_PER_MB = 1024 * 1024


def file_extension(path: str) -> str:
    """The lowercase extension without its dot, or `""` when there is none."""
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


async def image_content(
    backend: BackendProtocol | AsyncBackendProtocol,
    path: str,
    max_image_bytes: int,
) -> BinaryContent | str | None:
    """Read `path` as an image.

    Returns:
        `BinaryContent` for a recognized image within the size limit, an error
        message when it is missing or too large, or `None` when `path` is not an
        image at all and the caller should keep looking.
    """
    extension = file_extension(path)
    if extension not in IMAGE_EXTENSIONS:
        return None

    raw = await _read_within_limit(backend, path, max_image_bytes, "Image")
    if isinstance(raw, str):
        return raw

    media_type = IMAGE_MEDIA_TYPES.get(extension, "application/octet-stream")
    return BinaryContent(data=downscale_image(raw), media_type=media_type)  # pyright: ignore[reportCallIssue]


async def document_content(
    backend: BackendProtocol | AsyncBackendProtocol,
    path: str,
    max_document_bytes: int,
) -> BinaryContent | str | None:
    """Read `path` as a binary document.

    Returns:
        `BinaryContent` for a recognized document within the size limit, an
        error message when it is missing or too large, or `None` when `path` is
        not a document.
    """
    extension = file_extension(path)
    if extension not in DOCUMENT_EXTENSIONS:
        return None

    raw = await _read_within_limit(backend, path, max_document_bytes, "Document")
    if isinstance(raw, str):
        return raw

    return BinaryContent(data=raw, media_type=DOCUMENT_MEDIA_TYPES[extension])  # pyright: ignore[reportCallIssue]


def downscale_image(data: bytes, max_dim: int = DEFAULT_MAX_IMAGE_DIMENSION) -> bytes:
    """Shrink an oversized image so it fits the model's image budget.

    Returns the bytes unchanged when Pillow is missing, the image is already
    small enough, or it cannot be decoded — so this is always safe to call.
    """
    pillow = load_optional("PIL.Image")
    if pillow is None:  # pragma: no cover - Pillow is an optional extra
        return data

    try:
        with pillow.open(io.BytesIO(data)) as image:
            if max(image.size) <= max_dim:
                return data
            image_format = image.format or "PNG"
            image.thumbnail((max_dim, max_dim))
            buffer = io.BytesIO()
            image.save(buffer, format=image_format)
            return buffer.getvalue()
    except Exception:  # pragma: no cover - corrupt or unsupported image data
        return data


async def _read_within_limit(
    backend: BackendProtocol | AsyncBackendProtocol,
    path: str,
    max_bytes: int,
    kind: str,
) -> bytes | str:
    """Read raw bytes, refusing a missing, empty or oversized file.

    Args:
        backend: Backend to read from.
        path: File path to read.
        max_bytes: Largest acceptable size.
        kind: Content kind (`"Image"`, `"Document"`) for the error message.
    """
    raw = await ensure_async(backend).read_bytes(path)
    if not raw:
        return f"Error: {kind} file '{path}' not found or empty"
    if len(raw) > max_bytes:
        size_mb = len(raw) / BYTES_PER_MB
        limit_mb = max_bytes / BYTES_PER_MB
        return f"Error: {kind} '{path}' too large ({size_mb:.1f}MB, max {limit_mb:.1f}MB)"
    return raw
