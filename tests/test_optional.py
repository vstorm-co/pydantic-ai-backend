"""Tests for optional-dependency loading."""

import pytest

from pydantic_ai_backends import _optional


@pytest.fixture
def missing(monkeypatch: pytest.MonkeyPatch):
    """Make every optional import fail, as it would without the extra."""

    def explode(name: str):
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(_optional.importlib, "import_module", explode)


class TestLoad:
    def test_installed_module_is_returned(self):
        assert _optional.load("httpx", purpose="RemoteSandbox").__name__ == "httpx"

    def test_error_names_the_extra_and_the_purpose(self, missing):
        with pytest.raises(ImportError) as excinfo:
            _optional.load("chardet", purpose="encoding detection")

        message = str(excinfo.value)
        assert "chardet is required for encoding detection" in message
        assert "pip install pydantic-ai-backend[docker]" in message

    @pytest.mark.parametrize(
        ("module", "extra"),
        [
            ("docker", "docker"),
            ("pypdf", "docker"),
            ("httpx", "remote"),
            ("kubernetes", "kubernetes"),
            ("PIL", "images"),
        ],
    )
    def test_every_optional_module_maps_to_an_extra(self, missing, module: str, extra: str):
        with pytest.raises(ImportError, match=rf"pydantic-ai-backend\[{extra}\]"):
            _optional.load(module, purpose="testing")


class TestLoadOptional:
    def test_installed_module_is_returned(self):
        assert _optional.load_optional("json") is not None

    def test_absent_module_is_none(self, missing):
        assert _optional.load_optional("PIL.Image") is None
