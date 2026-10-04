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

    def test_lazy_import_a_workspace(self):
        """A workspace capability loads on first access, with Pydantic AI."""
        import pydantic_ai_backends

        assert pydantic_ai_backends.SandboxdWorkspace.__name__ == "SandboxdWorkspace"

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


def test_every_public_name_resolves():
    """`__all__` is the public API reference, so every entry must import."""
    import pydantic_ai_backends as package

    for name in package.__all__:
        assert getattr(package, name) is not None, name
