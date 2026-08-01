"""Tests for resolving a RuntimeConfig to a Docker image (no Docker needed)."""

import sys
import types
from pathlib import Path

import pytest

from pydantic_ai_backends.backends.docker import _image
from pydantic_ai_backends.backends.docker._image import (
    build_dockerfile,
    build_image,
    image_tag_for,
    prune_superseded_images,
    pull_if_absent,
    resolve_image,
)
from pydantic_ai_backends.types import RuntimeConfig

# Captured before the autouse fixture swaps the module attribute for a stub, so
# the probe itself can still be tested.
REAL_BUILDKIT_PROBE = _image.buildkit_available


@pytest.fixture
def fake_docker_errors(monkeypatch: pytest.MonkeyPatch):
    """Install a `docker.errors` module with the exceptions `build_image` uses."""
    errors = types.ModuleType("docker.errors")
    errors.ImageNotFound = type("ImageNotFound", (Exception,), {})
    docker = types.ModuleType("docker")
    docker.errors = errors
    monkeypatch.setitem(sys.modules, "docker", docker)
    monkeypatch.setitem(sys.modules, "docker.errors", errors)
    return errors


class _Image:
    def __init__(self, *tags: str):
        self.tags = list(tags)


class _Images:
    def __init__(
        self,
        missing: type[Exception] | None = None,
        existing: list[_Image] | None = None,
    ):
        self._missing = missing
        self.built: list[dict] = []
        self.requested: list[str] = []
        self.pulled: list[str] = []
        self.removed: list[str] = []
        self.existing = existing or []
        self.undeletable: set[str] = set()

    def get(self, tag: str):
        self.requested.append(tag)
        if self._missing is not None:
            raise self._missing(tag)
        return object()

    def build(self, **kwargs):
        self.built.append(kwargs)

    def pull(self, image: str):
        self.pulled.append(image)

    def list(self, name: str | None = None):
        return self.existing

    def remove(self, tag: str):
        if tag in self.undeletable:
            raise RuntimeError(f"image {tag} is in use by a container")
        self.removed.append(tag)


class _Client:
    def __init__(self, images: _Images):
        self.images = images


@pytest.fixture(autouse=True)
def classic_builder(monkeypatch: pytest.MonkeyPatch):
    """Default to the SDK's builder, which is what a host without the CLI has.

    Tests that care about BuildKit turn it back on for themselves — leaving it to
    whatever the machine running the suite happens to have installed would make
    these pass or fail by accident.
    """
    monkeypatch.setattr(_image, "buildkit_available", lambda: False)


class TestBuildDockerfile:
    """Dockerfile generation and input validation."""

    def _build(self, **kwargs) -> str:
        kwargs.setdefault("name", "test")
        kwargs.setdefault("base_image", "python:3.12-slim")
        return build_dockerfile(RuntimeConfig(**kwargs))

    def test_basic_pip_runtime(self):
        dockerfile = self._build(packages=["pandas", "numpy"])
        assert "FROM python:3.12-slim" in dockerfile
        assert "RUN pip install --no-cache-dir pandas numpy" in dockerfile
        assert "WORKDIR /workspace" in dockerfile

    def test_npm_installs_locally_not_global(self):
        """npm packages install into the work_dir, not globally (-g)."""
        dockerfile = self._build(
            packages=["react", "react-dom"],
            package_manager="npm",
            work_dir="/app",
        )
        assert "npm install react react-dom" in dockerfile
        assert "npm install -g" not in dockerfile

    def test_apt_and_cargo(self):
        apt = self._build(packages=["curl"], package_manager="apt")
        assert "apt-get update && apt-get install -y curl" in apt
        cargo = self._build(packages=["ripgrep"], package_manager="cargo")
        assert "cargo install ripgrep" in cargo

    def test_env_vars_quoted(self):
        assert "ENV FOO='bar baz'" in self._build(env_vars={"FOO": "bar baz"})

    def test_rejects_package_command_injection(self):
        with pytest.raises(ValueError, match="Invalid package name"):
            self._build(packages=["foo; rm -rf /"])

    def test_rejects_empty_package(self):
        with pytest.raises(ValueError, match="Invalid package name"):
            self._build(packages=[""])

    def test_rejects_env_value_newline(self):
        with pytest.raises(ValueError, match="newline"):
            self._build(env_vars={"FOO": "line1\nRUN evil"})

    def test_rejects_env_key(self):
        with pytest.raises(ValueError, match="environment variable name"):
            self._build(env_vars={"BAD KEY": "value"})

    def test_rejects_workdir_metacharacters(self):
        with pytest.raises(ValueError, match="work_dir"):
            self._build(work_dir="/app; rm -rf /")

    def test_rejects_setup_command_carriage_return(self):
        with pytest.raises(ValueError, match="setup command"):
            self._build(setup_commands=["echo hi\rRUN evil"])

    def test_rejects_setup_command_newline(self):
        with pytest.raises(ValueError, match="setup command"):
            self._build(setup_commands=["echo hi\nRUN evil"])

    def test_setup_command_emitted(self):
        assert "RUN echo hello" in self._build(setup_commands=["echo hello"])

    def test_scoped_npm_package_allowed(self):
        dockerfile = self._build(packages=["@types/react"], package_manager="npm", work_dir="/app")
        assert "@types/react" in dockerfile


class TestImageTag:
    def test_tag_tracks_the_runtime_configuration(self):
        """A changed runtime must not reuse the image built from the old one."""
        base = RuntimeConfig(name="ml", base_image="python:3.12-slim", packages=["numpy"])
        changed = base.model_copy(update={"packages": ["numpy", "pandas"]})

        assert image_tag_for(base).startswith("pydantic-ai-backend-runtime:ml-")
        assert image_tag_for(base) != image_tag_for(changed)
        assert image_tag_for(base) == image_tag_for(base.model_copy())


class TestResolveImage:
    def test_no_runtime_uses_the_fallback(self):
        client = _Client(_Images())
        assert resolve_image(client, None, "python:3.12-slim") == "python:3.12-slim"

    def test_ready_made_image_wins(self):
        runtime = RuntimeConfig(name="ds", image="registry/python-ds:v1")
        client = _Client(_Images())

        assert resolve_image(client, runtime, "ignored") == "registry/python-ds:v1"
        assert client.images.built == []

    def test_runtime_without_any_image_uses_the_fallback(self):
        runtime = RuntimeConfig(name="bare")
        assert resolve_image(_Client(_Images()), runtime, "node:20-slim") == "node:20-slim"

    def test_base_image_triggers_a_build(self, fake_docker_errors):
        runtime = RuntimeConfig(name="ml", base_image="python:3.12-slim", packages=["numpy"])
        client = _Client(_Images(missing=fake_docker_errors.ImageNotFound))

        tag = resolve_image(client, runtime, "unused")

        assert tag == image_tag_for(runtime)
        assert len(client.images.built) == 1
        assert client.images.built[0]["tag"] == tag


class TestBuildImage:
    def test_cached_image_is_reused(self, fake_docker_errors):
        runtime = RuntimeConfig(name="ml", base_image="python:3.12-slim")
        client = _Client(_Images())

        assert build_image(client, runtime) == image_tag_for(runtime)
        assert client.images.built == []

    def test_missing_cached_image_is_built(self, fake_docker_errors):
        runtime = RuntimeConfig(name="ml", base_image="python:3.12-slim")
        client = _Client(_Images(missing=fake_docker_errors.ImageNotFound))

        build_image(client, runtime)

        assert client.images.built[0]["rm"] is True
        assert b"FROM python:3.12-slim" in client.images.built[0]["fileobj"].getvalue()

    def test_cache_disabled_skips_the_lookup(self, fake_docker_errors):
        runtime = RuntimeConfig(name="ml", base_image="python:3.12-slim", cache_image=False)
        client = _Client(_Images())

        build_image(client, runtime)

        assert client.images.requested == []
        assert len(client.images.built) == 1


class TestCacheMounts:
    """Keeping the package cache between builds, which needs BuildKit."""

    def _build(self, *, cache_mounts: bool, **kwargs) -> str:
        kwargs.setdefault("name", "test")
        kwargs.setdefault("base_image", "python:3.12-slim")
        return build_dockerfile(RuntimeConfig(**kwargs), cache_mounts=cache_mounts)

    def test_off_by_default_because_the_classic_builder_rejects_them(self):
        """`--mount` is a hard error on the builder the Python SDK drives."""
        dockerfile = self._build(cache_mounts=False, packages=["numpy"])

        assert "--mount" not in dockerfile
        assert "# syntax" not in dockerfile
        assert "--no-cache-dir" in dockerfile

    def test_pip_caches_its_downloads_and_stops_discarding_them(self):
        dockerfile = self._build(cache_mounts=True, packages=["numpy"])

        assert dockerfile.startswith("# syntax=docker/dockerfile:1")
        assert "--mount=type=cache,target=/root/.cache/pip,sharing=locked" in dockerfile
        # The two fight: a cache mount is pointless if pip throws the cache away.
        assert "--no-cache-dir" not in dockerfile

    def test_npm_caches_its_store(self):
        dockerfile = self._build(cache_mounts=True, packages=["vite"], package_manager="npm")

        assert "--mount=type=cache,target=/root/.npm,sharing=locked" in dockerfile

    def test_cargo_caches_its_registry(self):
        dockerfile = self._build(cache_mounts=True, packages=["ripgrep"], package_manager="cargo")

        assert "--mount=type=cache,target=/usr/local/cargo/registry" in dockerfile

    def test_apt_caches_both_directories_and_disarms_docker_clean(self):
        """Debian's docker-clean hook would empty the mount right after install."""
        dockerfile = self._build(cache_mounts=True, packages=["curl"], package_manager="apt")

        assert "RUN rm -f /etc/apt/apt.conf.d/docker-clean" in dockerfile
        assert "target=/var/cache/apt,sharing=locked" in dockerfile
        assert "target=/var/lib/apt/lists,sharing=locked" in dockerfile
        assert "--no-install-recommends" in dockerfile

    def test_every_package_manager_has_a_cache_directory(self):
        from pydantic_ai_backends.backends.docker._image import PACKAGE_CACHES

        managers = set(RuntimeConfig.model_fields["package_manager"].annotation.__args__)

        assert managers <= set(PACKAGE_CACHES)


class TestBuilderSelection:
    """Which builder runs, and what each one is handed."""

    def test_buildkit_path_is_used_when_available(self, monkeypatch, fake_docker_errors):
        recorded: dict[str, str] = {}

        def fake_buildx(dockerfile: str, tag: str) -> None:
            recorded["dockerfile"] = dockerfile
            recorded["tag"] = tag

        monkeypatch.setattr(_image, "buildkit_available", lambda: True)
        monkeypatch.setattr(_image, "_build_with_buildx", fake_buildx)
        runtime = RuntimeConfig(name="ml", base_image="python:3.12-slim", packages=["numpy"])
        client = _Client(_Images(missing=fake_docker_errors.ImageNotFound))

        assert build_image(client, runtime) == image_tag_for(runtime)
        assert client.images.built == []  # the SDK builder is untouched
        assert "--mount=type=cache" in recorded["dockerfile"]
        assert recorded["tag"] == image_tag_for(runtime)

    def test_the_sdk_builder_never_sees_a_cache_mount(self, fake_docker_errors):
        runtime = RuntimeConfig(name="ml", base_image="python:3.12-slim", packages=["numpy"])
        client = _Client(_Images(missing=fake_docker_errors.ImageNotFound))

        build_image(client, runtime)

        sent = client.images.built[0]["fileobj"].getvalue().decode()
        assert "--mount" not in sent

    def test_a_failed_buildkit_build_carries_its_output(self, monkeypatch):
        import subprocess

        monkeypatch.setattr(
            _image.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no matching distribution"),
        )

        with pytest.raises(RuntimeError, match="no matching distribution"):
            _image._build_with_buildx("FROM scratch", "pab-test:1")

    def test_a_successful_buildkit_build_writes_a_context(self, monkeypatch):
        import subprocess

        seen: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            context = argv[-1]
            seen["dockerfile"] = (Path(context) / "Dockerfile").read_text()
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(_image.subprocess, "run", fake_run)

        _image._build_with_buildx("FROM scratch\n", "pab-test:1")

        assert "--load" in seen["argv"]
        assert "pab-test:1" in seen["argv"]
        assert seen["dockerfile"] == "FROM scratch\n"

    def test_no_docker_cli_means_no_buildkit(self, monkeypatch):
        monkeypatch.setattr(_image.shutil, "which", lambda _name: None)
        REAL_BUILDKIT_PROBE.cache_clear()
        try:
            assert REAL_BUILDKIT_PROBE() is False
        finally:
            REAL_BUILDKIT_PROBE.cache_clear()

    def test_a_working_cli_means_buildkit(self, monkeypatch):
        import subprocess

        monkeypatch.setattr(_image.shutil, "which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(
            _image.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b"")
        )
        REAL_BUILDKIT_PROBE.cache_clear()
        try:
            assert REAL_BUILDKIT_PROBE() is True
        finally:
            REAL_BUILDKIT_PROBE.cache_clear()

    def test_a_broken_cli_means_no_buildkit(self, monkeypatch):
        import subprocess

        monkeypatch.setattr(_image.shutil, "which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(
            _image.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"")
        )
        REAL_BUILDKIT_PROBE.cache_clear()
        try:
            assert REAL_BUILDKIT_PROBE() is False
        finally:
            REAL_BUILDKIT_PROBE.cache_clear()


class TestPullIfAbsent:
    def test_a_local_image_is_not_pulled(self, fake_docker_errors):
        client = _Client(_Images())

        assert pull_if_absent(client, "python:3.12-slim") is False
        assert client.images.pulled == []

    def test_a_missing_image_is_pulled(self, fake_docker_errors):
        client = _Client(_Images(missing=fake_docker_errors.ImageNotFound))

        assert pull_if_absent(client, "python:3.12-slim") is True
        assert client.images.pulled == ["python:3.12-slim"]


class TestPruningSupersededImages:
    """Every runtime edit mints a new tag, so the old image has to go somewhere."""

    def _runtime(self, *packages: str) -> RuntimeConfig:
        return RuntimeConfig(name="ml", base_image="python:3.12-slim", packages=list(packages))

    def test_an_image_from_an_earlier_configuration_is_removed(self):
        runtime = self._runtime("numpy")
        stale = image_tag_for(self._runtime("numpy", "pandas"))
        client = _Client(_Images(existing=[_Image(stale), _Image(image_tag_for(runtime))]))

        removed = prune_superseded_images(client, runtime)

        assert removed == [stale]

    def test_the_current_image_is_kept(self):
        runtime = self._runtime("numpy")
        client = _Client(_Images(existing=[_Image(image_tag_for(runtime))]))

        assert prune_superseded_images(client, runtime) == []

    def test_another_runtime_is_left_alone(self):
        runtime = self._runtime("numpy")
        other = RuntimeConfig(name="web", base_image="python:3.12-slim", packages=["fastapi"])
        client = _Client(_Images(existing=[_Image(image_tag_for(other))]))

        assert prune_superseded_images(client, runtime) == []

    def test_an_image_still_backing_a_container_is_left_alone(self):
        """A session opened before the edit is legitimately still using it."""
        runtime = self._runtime("numpy")
        stale = image_tag_for(self._runtime("numpy", "pandas"))
        images = _Images(existing=[_Image(stale)])
        images.undeletable = {stale}

        assert prune_superseded_images(_Client(images), runtime) == []
        assert images.removed == []

    def test_an_untagged_image_is_skipped(self):
        client = _Client(_Images(existing=[_Image()]))

        assert prune_superseded_images(client, self._runtime("numpy")) == []

    def test_a_listing_failure_does_not_fail_the_build(self):
        class Exploding(_Images):
            def list(self, name=None):
                raise RuntimeError("daemon gone")

        assert prune_superseded_images(_Client(Exploding()), self._runtime("numpy")) == []

    def test_a_build_prunes_as_a_side_effect(self, fake_docker_errors):
        runtime = self._runtime("numpy")
        stale = image_tag_for(self._runtime("numpy", "pandas"))
        images = _Images(missing=fake_docker_errors.ImageNotFound, existing=[_Image(stale)])

        build_image(_Client(images), runtime)

        assert images.removed == [stale]


class TestUnprivilegedRuntimes:
    """What `run_as_uid` adds to a generated image, and why each part is there."""

    def _runtime(self, **kwargs):
        from pydantic_ai_backends.types import RuntimeConfig

        return RuntimeConfig(name="nr", base_image="python:3.12-slim", **kwargs)

    def test_nothing_is_added_without_a_uid(self):
        from pydantic_ai_backends.backends.docker._image import build_dockerfile

        assert "useradd" not in build_dockerfile(self._runtime())

    def test_the_uid_gets_a_real_account(self):
        """A bare numeric id works for the kernel and breaks `getpwuid`."""
        from pydantic_ai_backends.backends.docker._image import SANDBOX_USER, build_dockerfile

        dockerfile = build_dockerfile(self._runtime(run_as_uid=1000))

        assert f"useradd --uid 1000 --create-home --shell /bin/bash {SANDBOX_USER}" in dockerfile

    def test_it_owns_a_virtualenv_that_comes_first_on_path(self):
        """Without one, pip installs somewhere off `PATH` and uv cannot install at all."""
        from pydantic_ai_backends.backends.docker._image import VENV_PATH, build_dockerfile

        dockerfile = build_dockerfile(self._runtime(run_as_uid=1000))

        assert f"python -m venv --system-site-packages {VENV_PATH}" in dockerfile
        assert f"chown -R 1000:1000 {VENV_PATH}" in dockerfile
        assert f"PATH={VENV_PATH}/bin:$PATH" in dockerfile

    def test_the_venv_keeps_the_runtimes_own_packages_importable(self):
        """They were installed system-wide, before the user existed."""
        from pydantic_ai_backends.backends.docker._image import build_dockerfile

        dockerfile = build_dockerfile(self._runtime(packages=["six"], run_as_uid=1000))
        install = dockerfile.index("pip install")
        venv = dockerfile.index("python -m venv")

        assert install < venv
        assert "--system-site-packages" in dockerfile

    def test_uv_is_pointed_at_the_venv_rather_than_the_interpreter(self):
        from pydantic_ai_backends.backends.docker._image import build_dockerfile

        assert "UV_SYSTEM_PYTHON=0" in build_dockerfile(self._runtime(run_as_uid=1000))

    def test_a_runtimes_own_env_still_wins(self):
        from pydantic_ai_backends.backends.docker._image import build_dockerfile

        dockerfile = build_dockerfile(self._runtime(run_as_uid=1000, env_vars={"HOME": "/tmp"}))

        assert dockerfile.index("HOME=/home/agent") < dockerfile.index("ENV HOME=/tmp")

    @pytest.mark.parametrize("uid", [0, -1])
    def test_a_uid_that_is_not_one_is_refused(self, uid: int):
        """Root defeats the purpose, and the value lands in a shell command."""
        from pydantic_ai_backends.backends.docker._image import unprivileged_instructions

        with pytest.raises(ValueError, match="positive uid"):
            unprivileged_instructions(uid)
