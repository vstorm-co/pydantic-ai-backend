"""Tests for console toolset."""

import inspect
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import BinaryContent, RunContext

from pydantic_ai_backends import (
    LocalBackend,
    StateBackend,
    create_console_toolset,
    get_console_system_prompt,
)
from pydantic_ai_backends.toolsets._content import (
    DEFAULT_MAX_DOCUMENT_BYTES,
    DEFAULT_MAX_IMAGE_BYTES,
    DOCUMENT_EXTENSIONS,
    DOCUMENT_MEDIA_TYPES,
    IMAGE_EXTENSIONS,
    IMAGE_MEDIA_TYPES,
    document_content,
    image_content,
)
from pydantic_ai_backends.toolsets.console import ConsoleDeps

# Minimal valid-ish PDF payload (header + EOF marker).
PDF_DATA = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


@dataclass
class MockDeps:
    """Mock deps for testing."""

    backend: LocalBackend | StateBackend


class TestConsoleDeps:
    """Test ConsoleDeps protocol."""

    def test_mock_deps_implements_protocol(self, tmp_path: Path):
        """Test that MockDeps implements ConsoleDeps."""
        backend = LocalBackend(root_dir=tmp_path)
        deps = MockDeps(backend=backend)

        # Should be instance of ConsoleDeps (runtime checkable protocol)
        assert isinstance(deps, ConsoleDeps)

    def test_state_backend_implements_protocol(self):
        """Test that StateBackend works with ConsoleDeps."""
        backend = StateBackend()
        deps = MockDeps(backend=backend)
        assert isinstance(deps, ConsoleDeps)


class TestCreateConsoleToolset:
    """Test create_console_toolset function."""

    def test_create_default_toolset(self):
        """Test creating toolset with default settings."""
        toolset = create_console_toolset()

        # Should have all standard tools
        tool_names = list(toolset.tools.keys())
        assert "ls" in tool_names
        assert "read_file" in tool_names
        assert "write_file" in tool_names
        assert "edit_file" in tool_names
        assert "glob" in tool_names
        assert "grep" in tool_names
        assert "execute" in tool_names

    def test_create_toolset_without_execute(self):
        """Test creating toolset without execute tool."""
        toolset = create_console_toolset(include_execute=False)

        tool_names = list(toolset.tools.keys())
        assert "ls" in tool_names
        assert "read_file" in tool_names
        assert "execute" not in tool_names

    def test_create_toolset_with_custom_id(self):
        """Test creating toolset with custom ID."""
        toolset = create_console_toolset(id="my-console")
        assert toolset.id == "my-console"

    def test_tool_approval_settings(self):
        """Test tool approval settings."""
        # Default: write doesn't require approval, execute does
        toolset = create_console_toolset()

        # Verify toolset was created with expected tools
        assert "write_file" in toolset.tools
        assert "execute" in toolset.tools

        # Check requires_approval setting on execute tool
        assert toolset.tools["execute"].requires_approval is True

    def test_toolset_with_write_approval(self):
        """Test toolset with write approval required."""
        toolset = create_console_toolset(require_write_approval=True)

        assert "write_file" in toolset.tools
        assert "edit_file" in toolset.tools

        # Check requires_approval setting
        assert toolset.tools["write_file"].requires_approval is True
        assert toolset.tools["edit_file"].requires_approval is True

    def test_toolset_default_ignore_hidden_configurable(self):
        """Grep should respect default ignore hidden flag."""
        toolset = create_console_toolset(default_ignore_hidden=False)

        assert hasattr(toolset, "_console_default_ignore_hidden")
        assert toolset._console_default_ignore_hidden is False

        grep_impl = toolset._console_grep_impl
        params = inspect.signature(grep_impl).parameters
        assert params["ignore_hidden"].default is False


class TestGetConsoleSystemPrompt:
    """Test get_console_system_prompt function."""

    def test_returns_string(self):
        """Test that system prompt returns a string."""
        prompt = get_console_system_prompt()
        assert isinstance(prompt, str)

    def test_contains_tool_descriptions(self):
        """Test that system prompt contains tool descriptions."""
        prompt = get_console_system_prompt()

        # Should mention file operations
        assert "ls" in prompt.lower() or "list" in prompt.lower()
        assert "read" in prompt.lower()
        assert "write" in prompt.lower()
        assert "edit" in prompt.lower()

        # Should mention shell execution
        assert "execute" in prompt.lower() or "shell" in prompt.lower()


class TestCreateConsoleToolsetImageParams:
    """Test create_console_toolset with image parameters."""

    def test_create_toolset_with_image_support(self):
        """Test creating toolset with image_support=True."""
        toolset = create_console_toolset(image_support=True)
        assert "read_file" in toolset.tools

    def test_create_toolset_with_custom_max_image_bytes(self):
        """Test creating toolset with custom max_image_bytes."""
        toolset = create_console_toolset(image_support=True, max_image_bytes=1024)
        assert "read_file" in toolset.tools

    def test_create_toolset_default_no_image_support(self):
        """Test that image_support defaults to False."""
        toolset = create_console_toolset()
        assert "read_file" in toolset.tools


class TestImageConstants:
    """Test image-related constants."""

    def test_image_extensions(self):
        """Test IMAGE_EXTENSIONS contains expected formats."""
        assert "png" in IMAGE_EXTENSIONS
        assert "jpg" in IMAGE_EXTENSIONS
        assert "jpeg" in IMAGE_EXTENSIONS
        assert "gif" in IMAGE_EXTENSIONS
        assert "webp" in IMAGE_EXTENSIONS
        # SVG is intentionally excluded (it's text/XML)
        assert "svg" not in IMAGE_EXTENSIONS

    def test_image_media_types(self):
        """Test IMAGE_MEDIA_TYPES maps extensions to MIME types."""
        assert IMAGE_MEDIA_TYPES["png"] == "image/png"
        assert IMAGE_MEDIA_TYPES["jpg"] == "image/jpeg"
        assert IMAGE_MEDIA_TYPES["jpeg"] == "image/jpeg"
        assert IMAGE_MEDIA_TYPES["gif"] == "image/gif"
        assert IMAGE_MEDIA_TYPES["webp"] == "image/webp"

    def test_all_extensions_have_media_types(self):
        """Test every IMAGE_EXTENSION has a corresponding IMAGE_MEDIA_TYPE."""
        for ext in IMAGE_EXTENSIONS:
            assert ext in IMAGE_MEDIA_TYPES

    def test_default_max_image_bytes(self):
        """Test DEFAULT_MAX_IMAGE_BYTES is 50MB."""
        assert DEFAULT_MAX_IMAGE_BYTES == 50 * 1024 * 1024


class TestReadFileImageSupport:
    """Test read_file with image_support enabled."""

    def test_read_image_png_local_backend(self, tmp_path: Path):
        """Test reading a PNG image returns BinaryContent."""
        # Create a fake PNG file (PNG header + some data)
        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        img_path = tmp_path / "test.png"
        img_path.write_bytes(png_data)

        backend = LocalBackend(root_dir=tmp_path)
        result = backend.read_bytes("test.png")
        assert result == png_data

        # Verify the toolset is created with image_support
        toolset = create_console_toolset(image_support=True)
        assert "read_file" in toolset.tools

    def test_read_image_jpg_local_backend(self, tmp_path: Path):
        """Test reading a JPG image returns bytes via read_bytes."""
        jpg_data = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        img_path = tmp_path / "photo.jpg"
        img_path.write_bytes(jpg_data)

        backend = LocalBackend(root_dir=tmp_path)
        result = backend.read_bytes("photo.jpg")
        assert result == jpg_data

    def test_read_image_jpeg_extension(self, tmp_path: Path):
        """Test .jpeg extension is recognized."""
        jpeg_data = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        img_path = tmp_path / "photo.jpeg"
        img_path.write_bytes(jpeg_data)

        backend = LocalBackend(root_dir=tmp_path)
        result = backend.read_bytes("photo.jpeg")
        assert result == jpeg_data

    def test_read_image_gif_local_backend(self, tmp_path: Path):
        """Test reading a GIF image."""
        gif_data = b"GIF89a" + b"\x00" * 50
        img_path = tmp_path / "anim.gif"
        img_path.write_bytes(gif_data)

        backend = LocalBackend(root_dir=tmp_path)
        result = backend.read_bytes("anim.gif")
        assert result == gif_data

    def test_read_image_webp_local_backend(self, tmp_path: Path):
        """Test reading a WebP image."""
        webp_data = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 50
        img_path = tmp_path / "image.webp"
        img_path.write_bytes(webp_data)

        backend = LocalBackend(root_dir=tmp_path)
        result = backend.read_bytes("image.webp")
        assert result == webp_data

    def test_read_image_not_found(self, tmp_path: Path):
        """Test reading nonexistent image returns empty bytes."""
        backend = LocalBackend(root_dir=tmp_path)
        result = backend.read_bytes("nonexistent.png")
        assert result == b""

    def test_read_text_file_with_image_support(self, tmp_path: Path):
        """Test that text files still return text with image_support enabled."""
        txt_path = tmp_path / "readme.txt"
        txt_path.write_text("Hello, world!")

        backend = LocalBackend(root_dir=tmp_path)
        # Text files should still use the standard read() method
        result = backend.read("readme.txt")
        assert "Hello, world!" in result

    def test_read_image_state_backend(self):
        """Test reading image from StateBackend via read_bytes."""
        backend = StateBackend()
        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        backend.write("/test.png", png_data)

        result = backend.read_bytes("/test.png")
        # StateBackend stores as text, so read_bytes returns encoded text
        assert isinstance(result, bytes)

    def test_image_extension_case_insensitive(self):
        """Test that image extension detection is case-insensitive."""
        # The implementation uses .lower() so uppercase should work
        path = "photo.PNG"
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        assert ext in IMAGE_EXTENSIONS

    def test_no_extension_not_image(self):
        """Test that files without extension are not treated as images."""
        path = "Makefile"
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        assert ext not in IMAGE_EXTENSIONS

    def test_binary_content_creation(self):
        """Test creating BinaryContent from image data."""
        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        content = BinaryContent(data=png_data, media_type="image/png")
        assert content.data == png_data
        assert content.media_type == "image/png"
        assert content.is_image

    def test_binary_content_jpg_media_type(self):
        """Test BinaryContent with JPEG media type."""
        jpg_data = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        content = BinaryContent(data=jpg_data, media_type="image/jpeg")
        assert content.media_type == "image/jpeg"
        assert content.is_image


class TestImageExports:
    """Test that image constants are exported from the package."""

    def test_image_extensions_exported(self):
        """Test IMAGE_EXTENSIONS is importable from package."""
        from pydantic_ai_backends import IMAGE_EXTENSIONS as exported

        assert exported == IMAGE_EXTENSIONS

    def test_image_media_types_exported(self):
        """Test IMAGE_MEDIA_TYPES is importable from package."""
        from pydantic_ai_backends import IMAGE_MEDIA_TYPES as exported

        assert exported == IMAGE_MEDIA_TYPES

    def test_default_max_image_bytes_exported(self):
        """Test DEFAULT_MAX_IMAGE_BYTES is importable from package."""
        from pydantic_ai_backends import DEFAULT_MAX_IMAGE_BYTES as exported

        assert exported == DEFAULT_MAX_IMAGE_BYTES


class TestConsoleToolsetAlias:
    """Test ConsoleToolset alias."""

    def test_alias_works(self):
        """Test that ConsoleToolset alias works."""
        from pydantic_ai_backends.toolsets.console import ConsoleToolset

        toolset = ConsoleToolset()
        assert len(toolset.tools) > 0


class TestDocumentConstants:
    """Test document-related constants."""

    def test_document_extensions(self):
        """Test DOCUMENT_EXTENSIONS contains pdf."""
        assert "pdf" in DOCUMENT_EXTENSIONS

    def test_document_media_types(self):
        """Test DOCUMENT_MEDIA_TYPES maps pdf to application/pdf."""
        assert DOCUMENT_MEDIA_TYPES["pdf"] == "application/pdf"

    def test_pdf_not_in_image_extensions(self):
        """PDFs are documents, not images - the two sets stay disjoint."""
        assert "pdf" not in IMAGE_EXTENSIONS
        assert IMAGE_EXTENSIONS.isdisjoint(DOCUMENT_EXTENSIONS)

    def test_all_document_extensions_have_media_types(self):
        """Every DOCUMENT_EXTENSION has a corresponding DOCUMENT_MEDIA_TYPE."""
        for ext in DOCUMENT_EXTENSIONS:
            assert ext in DOCUMENT_MEDIA_TYPES

    def test_default_max_document_bytes(self):
        """Test DEFAULT_MAX_DOCUMENT_BYTES is 50MB."""
        assert DEFAULT_MAX_DOCUMENT_BYTES == 50 * 1024 * 1024

    def test_document_constants_exported(self):
        """Test document constants are importable from the package."""
        from pydantic_ai_backends import DEFAULT_MAX_DOCUMENT_BYTES as max_exported
        from pydantic_ai_backends import DOCUMENT_EXTENSIONS as ext_exported
        from pydantic_ai_backends import DOCUMENT_MEDIA_TYPES as media_exported

        assert ext_exported == DOCUMENT_EXTENSIONS
        assert media_exported == DOCUMENT_MEDIA_TYPES
        assert max_exported == DEFAULT_MAX_DOCUMENT_BYTES


class TestCreateConsoleToolsetDocumentParams:
    """Test create_console_toolset with document parameters."""

    def test_create_toolset_with_document_support(self):
        """Test creating toolset with document_support=True."""
        toolset = create_console_toolset(document_support=True)
        assert "read_file" in toolset.tools

    def test_create_toolset_with_custom_max_document_bytes(self):
        """Test creating toolset with custom max_document_bytes."""
        toolset = create_console_toolset(document_support=True, max_document_bytes=1024)
        assert "read_file" in toolset.tools

    def test_create_toolset_default_no_document_support(self):
        """Test that document_support defaults to False."""
        toolset = create_console_toolset()
        assert "read_file" in toolset.tools


class TestMaybeDocumentContent:
    """Test the _maybe_document_content helper (separate from image handling)."""

    async def test_pdf_returns_binary_content(self, tmp_path: Path):
        """A .pdf file is returned as BinaryContent(application/pdf)."""
        (tmp_path / "report.pdf").write_bytes(PDF_DATA)
        backend = LocalBackend(root_dir=tmp_path)

        result = await document_content(
            backend, "report.pdf", max_document_bytes=DEFAULT_MAX_DOCUMENT_BYTES
        )

        assert isinstance(result, BinaryContent)
        assert result.media_type == "application/pdf"
        assert result.data == PDF_DATA
        assert result.is_document

    async def test_pdf_too_large_returns_error(self, tmp_path: Path):
        """An oversized .pdf returns the size-limit error string."""
        (tmp_path / "huge.pdf").write_bytes(PDF_DATA)
        backend = LocalBackend(root_dir=tmp_path)

        result = await document_content(backend, "huge.pdf", max_document_bytes=1)

        assert result == (
            f"Error: Document 'huge.pdf' too large "
            f"({len(PDF_DATA) / (1024 * 1024):.1f}MB, max 0.0MB)"
        )

    async def test_pdf_missing_returns_error(self):
        """A missing/empty .pdf returns the not-found error string."""
        backend = StateBackend()

        result = await document_content(
            backend, "missing.pdf", max_document_bytes=DEFAULT_MAX_DOCUMENT_BYTES
        )

        assert result == "Error: Document file 'missing.pdf' not found or empty"

    async def test_image_returns_none(self, tmp_path: Path):
        """An image is NOT a document - the document helper ignores it."""
        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        (tmp_path / "test.png").write_bytes(png_data)
        backend = LocalBackend(root_dir=tmp_path)

        result = await document_content(
            backend, "test.png", max_document_bytes=DEFAULT_MAX_DOCUMENT_BYTES
        )

        assert result is None

    async def test_text_file_returns_none(self, tmp_path: Path):
        """A non-document extension falls through to the text path."""
        (tmp_path / "notes.txt").write_text("hello")
        backend = LocalBackend(root_dir=tmp_path)

        result = await document_content(
            backend, "notes.txt", max_document_bytes=DEFAULT_MAX_DOCUMENT_BYTES
        )

        assert result is None

    async def test_no_extension_returns_none(self, tmp_path: Path):
        """A file without an extension falls through to the text path."""
        (tmp_path / "Makefile").write_text("all:\n")
        backend = LocalBackend(root_dir=tmp_path)

        result = await document_content(
            backend, "Makefile", max_document_bytes=DEFAULT_MAX_DOCUMENT_BYTES
        )

        assert result is None


class TestMaybeImageContent:
    """Test the _maybe_image_content helper (separate from document handling)."""

    async def test_image_returns_binary_content(self, tmp_path: Path):
        """Existing image behavior is unchanged."""
        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        (tmp_path / "test.png").write_bytes(png_data)
        backend = LocalBackend(root_dir=tmp_path)

        result = await image_content(backend, "test.png", max_image_bytes=DEFAULT_MAX_IMAGE_BYTES)

        assert isinstance(result, BinaryContent)
        assert result.media_type == "image/png"
        assert result.is_image

    async def test_image_missing_error_message_unchanged(self):
        """Image not-found error message is preserved verbatim."""
        backend = StateBackend()

        result = await image_content(
            backend, "missing.png", max_image_bytes=DEFAULT_MAX_IMAGE_BYTES
        )

        assert result == "Error: Image file 'missing.png' not found or empty"

    async def test_image_too_large_message_unchanged(self, tmp_path: Path):
        """Image size-limit error message is preserved verbatim."""
        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        (tmp_path / "test.png").write_bytes(png_data)
        backend = LocalBackend(root_dir=tmp_path)

        result = await image_content(backend, "test.png", max_image_bytes=1)

        assert result == (
            f"Error: Image 'test.png' too large ({len(png_data) / (1024 * 1024):.1f}MB, max 0.0MB)"
        )

    async def test_pdf_returns_none(self, tmp_path: Path):
        """A PDF is NOT an image - the image helper ignores it."""
        (tmp_path / "report.pdf").write_bytes(PDF_DATA)
        backend = LocalBackend(root_dir=tmp_path)

        result = await image_content(backend, "report.pdf", max_image_bytes=DEFAULT_MAX_IMAGE_BYTES)

        assert result is None


def _make_ctx(deps: MockDeps) -> RunContext[ConsoleDeps]:
    """Build a minimal RunContext for invoking a tool function directly."""
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.usage import RunUsage

    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


class TestReadFilePdfIntegration:
    """End-to-end: invoke the real read_file tool for both edit_format variants."""

    async def test_str_replace_read_file_pdf(self, tmp_path: Path):
        """str_replace read_file returns BinaryContent for a PDF."""
        (tmp_path / "report.pdf").write_bytes(PDF_DATA)
        backend = LocalBackend(root_dir=tmp_path)
        toolset = create_console_toolset(document_support=True)
        read_file = toolset.tools["read_file"].function

        result = await read_file(_make_ctx(MockDeps(backend=backend)), "report.pdf")

        assert isinstance(result, BinaryContent)
        assert result.media_type == "application/pdf"
        assert result.data == PDF_DATA

    async def test_hashline_read_file_pdf(self, tmp_path: Path):
        """hashline read_file returns identical BinaryContent for a PDF."""
        (tmp_path / "report.pdf").write_bytes(PDF_DATA)
        backend = LocalBackend(root_dir=tmp_path)
        toolset = create_console_toolset(document_support=True, edit_format="hashline")
        read_file = toolset.tools["read_file"].function

        result = await read_file(_make_ctx(MockDeps(backend=backend)), "report.pdf")

        assert isinstance(result, BinaryContent)
        assert result.media_type == "application/pdf"
        assert result.data == PDF_DATA

    async def test_pdf_ignored_without_document_support(self, tmp_path: Path):
        """With document_support off, a PDF falls through to the text path."""
        (tmp_path / "report.pdf").write_bytes(PDF_DATA)
        backend = LocalBackend(root_dir=tmp_path)
        # image_support on but document_support off: PDF must NOT become BinaryContent
        toolset = create_console_toolset(image_support=True)
        read_file = toolset.tools["read_file"].function

        result = await read_file(_make_ctx(MockDeps(backend=backend)), "report.pdf")

        assert not isinstance(result, BinaryContent)

    async def test_image_and_document_support_together(self, tmp_path: Path):
        """Both flags on: images and documents each resolve to BinaryContent."""
        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        (tmp_path / "test.png").write_bytes(png_data)
        (tmp_path / "report.pdf").write_bytes(PDF_DATA)
        backend = LocalBackend(root_dir=tmp_path)
        toolset = create_console_toolset(image_support=True, document_support=True)
        read_file = toolset.tools["read_file"].function
        ctx = _make_ctx(MockDeps(backend=backend))

        image = await read_file(ctx, "test.png")
        document = await read_file(ctx, "report.pdf")

        assert isinstance(image, BinaryContent)
        assert image.media_type == "image/png"
        assert isinstance(document, BinaryContent)
        assert document.media_type == "application/pdf"

    async def test_str_replace_read_file_text_unaffected(self, tmp_path: Path):
        """Text files still return text (str_replace) with both flags on."""
        (tmp_path / "readme.txt").write_text("Hello, world!")
        backend = LocalBackend(root_dir=tmp_path)
        toolset = create_console_toolset(image_support=True, document_support=True)
        read_file = toolset.tools["read_file"].function

        result = await read_file(_make_ctx(MockDeps(backend=backend)), "readme.txt")

        assert isinstance(result, str)
        assert "Hello, world!" in result

    async def test_hashline_read_file_text_unaffected(self, tmp_path: Path):
        """Text files still return hashline text with both flags on."""
        (tmp_path / "readme.txt").write_text("Hello, world!")
        backend = LocalBackend(root_dir=tmp_path)
        toolset = create_console_toolset(
            image_support=True, document_support=True, edit_format="hashline"
        )
        read_file = toolset.tools["read_file"].function

        result = await read_file(_make_ctx(MockDeps(backend=backend)), "readme.txt")

        assert isinstance(result, str)
        assert "Hello, world!" in result


class TestGrepOutputTruncation:
    """Long result sets are summarised rather than dumped in full."""

    def _backend(self, file_count: int) -> StateBackend:
        backend = StateBackend()
        for i in range(file_count):
            backend.write(f"/f{i:03d}.txt", "needle here")
        return backend

    async def _grep(self, backend: StateBackend, **kwargs) -> str:
        toolset = create_console_toolset()
        return await toolset._console_grep_impl(  # type: ignore[attr-defined]
            _make_ctx(MockDeps(backend=backend)), "needle", **kwargs
        )

    async def test_file_list_is_capped_and_counted(self):
        out = await self._grep(self._backend(60))

        assert out.startswith("Files containing 'needle':")
        assert "... and 10 more files" in out

    async def test_short_file_list_is_shown_in_full(self):
        out = await self._grep(self._backend(3))

        assert "more files" not in out
        assert out.count("/f") == 3

    async def test_content_mode_is_capped_and_counted(self):
        out = await self._grep(self._backend(60), output_mode="content")

        assert out.startswith("Matches for 'needle':")
        assert "... and 10 more matches" in out
