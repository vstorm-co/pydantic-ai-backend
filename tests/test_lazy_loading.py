"""Tests for lazy loading of optional dependencies."""

import pytest


class TestLazyLoading:
    """Tests for lazy import functionality."""

    def test_lazy_import_docker_sandbox(self):
        """Test lazy import of DockerSandbox."""
        import pydantic_ai_backends

        # This should trigger lazy loading
        DockerSandbox = pydantic_ai_backends.DockerSandbox
        assert DockerSandbox is not None
        assert DockerSandbox.__name__ == "DockerSandbox"

    def test_lazy_import_base_sandbox(self):
        """Test lazy import of BaseSandbox."""
        import pydantic_ai_backends

        BaseSandbox = pydantic_ai_backends.BaseSandbox
        assert BaseSandbox is not None
        assert BaseSandbox.__name__ == "BaseSandbox"

    def test_lazy_import_session_manager(self):
        """Test lazy import of SessionManager."""
        import pydantic_ai_backends

        SessionManager = pydantic_ai_backends.SessionManager
        assert SessionManager is not None
        assert SessionManager.__name__ == "SessionManager"

    def test_lazy_import_builtin_runtimes(self):
        """Test lazy import of BUILTIN_RUNTIMES."""
        import pydantic_ai_backends

        BUILTIN_RUNTIMES = pydantic_ai_backends.BUILTIN_RUNTIMES
        assert BUILTIN_RUNTIMES is not None
        assert isinstance(BUILTIN_RUNTIMES, dict)

    def test_lazy_import_get_runtime(self):
        """Test lazy import of get_runtime."""
        import pydantic_ai_backends

        get_runtime = pydantic_ai_backends.get_runtime
        assert get_runtime is not None
        assert callable(get_runtime)

    def test_lazy_import_invalid_name(self):
        """Test lazy import of invalid name raises AttributeError."""
        import pydantic_ai_backends

        with pytest.raises(AttributeError) as excinfo:
            _ = pydantic_ai_backends.NonExistentClass
        assert "NonExistentClass" in str(excinfo.value)


def test_adapter_helpers_are_importable_from_the_root():
    """0.2.17 documented `is_async_backend` but exported only its sibling."""
    import pydantic_ai_backends as package

    for name in ("ensure_async", "is_async_backend"):
        assert name in package.__all__
        assert getattr(package, name).__name__ == name
