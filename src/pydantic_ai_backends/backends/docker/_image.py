"""Resolving a `RuntimeConfig` to a Docker image, building one when needed.

Every value that reaches a generated Dockerfile is validated or quoted first.
A runtime is often assembled from configuration or user input, and an
unescaped value in a `RUN` line runs as a command at build time.
"""

from __future__ import annotations

import functools
import hashlib
import io
import logging
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai_backends.types import RuntimeConfig

if TYPE_CHECKING:
    from docker import DockerClient

_logger = logging.getLogger(__name__)

IMAGE_REPOSITORY = "pydantic-ai-backend-runtime"
"""Repository every built runtime image is tagged under."""

BUILDX_PROBE_TIMEOUT = 10
"""Seconds allowed for the one-off `docker buildx version` check."""

PACKAGE_CACHES: dict[str, tuple[str, ...]] = {
    "pip": ("/root/.cache/pip",),
    "npm": ("/root/.npm",),
    "apt": ("/var/cache/apt", "/var/lib/apt/lists"),
    "cargo": ("/usr/local/cargo/registry",),
}
"""Directories worth keeping between builds, per package manager.

Only reachable through BuildKit cache mounts, so only used on that path — the
classic builder rejects `--mount` outright.
"""

PACKAGE_NAME = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._@/+=<>~!\[\]-]*$")
"""Accepts pip names with extras and specifiers, npm `@scope/name`, apt and
cargo names — and rejects whitespace and shell metacharacters."""

ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""The POSIX portable character set for environment variable names."""

SHELL_METACHARACTERS = frozenset(";&|`$()<>\n\r")
"""Characters that would let an interpolated value start its own command."""


def pull_if_absent(client: DockerClient, image: str) -> bool:
    """Pull a ready-made image unless it is already local.

    Returns:
        Whether a pull actually happened, so a caller can log the slow case.
    """
    import docker.errors

    try:
        client.images.get(image)
        return False
    except docker.errors.ImageNotFound:
        client.images.pull(image)
        return True


def resolve_image(
    client: DockerClient,
    runtime: RuntimeConfig | None,
    fallback_image: str,
) -> str:
    """Return the image to run, building it when the runtime describes one.

    Args:
        client: Docker client used for the cache lookup and the build.
        runtime: Runtime to satisfy, or `None` to use `fallback_image`.
        fallback_image: Image to use when the runtime names none.
    """
    if runtime is None:
        return fallback_image
    if runtime.image:
        return runtime.image
    if runtime.base_image:
        return build_image(client, runtime)
    return fallback_image


def build_image(client: DockerClient, runtime: RuntimeConfig) -> str:
    """Build (or reuse) an image with the runtime's packages installed.

    Uses BuildKit through the `docker buildx` CLI when it is available, which is
    what makes package caches survive between builds; falls back to the SDK's
    classic builder otherwise, since that one rejects `--mount` outright.

    Images superseded by this build are removed afterwards — the tag embeds a
    digest of the runtime, so editing one would otherwise orphan its predecessor
    on disk for good.

    Returns:
        The image tag, which embeds a digest of the runtime so a changed
        configuration builds a fresh image instead of reusing a stale one.
    """
    import docker.errors

    image_tag = image_tag_for(runtime)

    if runtime.cache_image:
        try:
            client.images.get(image_tag)
            return image_tag
        except docker.errors.ImageNotFound:
            pass

    if buildkit_available():
        _build_with_buildx(build_dockerfile(runtime, cache_mounts=True), image_tag)
    else:
        client.images.build(
            fileobj=io.BytesIO(build_dockerfile(runtime).encode()),
            tag=image_tag,
            rm=True,
        )

    prune_superseded_images(client, runtime)
    return image_tag


@functools.lru_cache(maxsize=1)
def buildkit_available() -> bool:
    """Whether `docker buildx` can be driven from here.

    Probed once per process. The Python SDK has no BuildKit support at all, so
    the CLI is the only route to cache mounts — and a deployment that talks to a
    mounted socket from a slim image may well not have the CLI installed, which
    is why this is a question rather than an assumption.
    """
    binary = shutil.which("docker")
    if binary is None:
        return False
    try:
        probe = subprocess.run(
            [binary, "buildx", "version"],
            capture_output=True,
            timeout=BUILDX_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - probe is best effort
        return False
    return probe.returncode == 0


def _build_with_buildx(dockerfile: str, image_tag: str) -> None:
    """Build one image with BuildKit, loading it into the local image store.

    Raises:
        RuntimeError: If the build fails, carrying buildx's own output — the
            caller has no other way to see why a package would not install.
    """
    with tempfile.TemporaryDirectory(prefix="pab-build-") as context:
        # An empty context: everything the build needs is in the Dockerfile, and
        # sending a directory of unrelated files to the daemon would be waste.
        Path(context, "Dockerfile").write_text(dockerfile, encoding="utf-8")
        result = subprocess.run(
            ["docker", "buildx", "build", "--load", "--tag", image_tag, context],
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"Building {image_tag} failed:\n{result.stderr.strip()}")


def prune_superseded_images(client: DockerClient, runtime: RuntimeConfig) -> list[str]:
    """Remove images this runtime built before its configuration changed.

    The tag carries a digest of the runtime, so every edit mints a new one and
    leaves the previous image behind — a few hundred megabytes each, and nothing
    else reclaims them.

    An image still backing a container is left alone: `remove` refuses it, and a
    session started before the edit is legitimately still using it.

    Returns:
        The tags actually removed.
    """
    current = image_tag_for(runtime)
    prefix = f"{IMAGE_REPOSITORY}:{runtime.name}-"
    removed: list[str] = []

    try:
        images = client.images.list(name=IMAGE_REPOSITORY)
    except Exception:  # pragma: no cover - listing failure must not fail a build
        return removed

    for image in images:
        for tag in getattr(image, "tags", []) or []:
            if not tag.startswith(prefix) or tag == current:
                continue
            try:
                client.images.remove(tag)
            except Exception:
                # In use by a running container, or already gone.
                continue
            removed.append(tag)

    if removed:
        _logger.info("Removed %d superseded image(s) for runtime %s", len(removed), runtime.name)
    return removed


def image_tag_for(runtime: RuntimeConfig) -> str:
    """Tag identifying the image this runtime builds to.

    The digest is not a security boundary, hence `usedforsecurity=False` —
    plain `md5()` is unavailable on FIPS-enforcing hosts.
    """
    digest = hashlib.md5(runtime.model_dump_json().encode(), usedforsecurity=False)
    return f"{IMAGE_REPOSITORY}:{runtime.name}-{digest.hexdigest()[:12]}"


def build_dockerfile(runtime: RuntimeConfig, *, cache_mounts: bool = False) -> str:
    """Render the Dockerfile for a runtime with a resolved `base_image`.

    Args:
        runtime: Runtime to render, which must name a `base_image`.
        cache_mounts: Keep the package manager's download cache between builds
            with `RUN --mount=type=cache`. Only valid under BuildKit — the
            classic builder fails on the option — so it is off by default and
            :func:`build_image` turns it on only when it has BuildKit.

    Raises:
        ValueError: If a package name, environment variable or work directory
            holds something that would escape the instruction it lands in.
    """
    assert runtime.base_image is not None
    lines: list[str] = []
    if cache_mounts:
        # Required for `--mount` to parse, and harmless on the classic builder's
        # side because that path never sees this file.
        lines.append("# syntax=docker/dockerfile:1")
    lines.append(f"FROM {runtime.base_image}")

    for command in runtime.setup_commands:
        # Setup commands are author-controlled shell snippets, so only newlines
        # are rejected — those would smuggle extra instructions into the file.
        if "\n" in command or "\r" in command:
            raise ValueError("setup command contains a newline")
        lines.append(f"RUN {command}")

    if runtime.packages:
        lines.extend(install_instructions(runtime, cache_mounts=cache_mounts))

    for key, value in runtime.env_vars.items():
        lines.append(env_instruction(key, value))

    reject_metacharacters(runtime.work_dir, what="work_dir")
    lines.append(f"WORKDIR {shlex.quote(runtime.work_dir)}")

    return "\n".join(lines)


def install_instructions(runtime: RuntimeConfig, *, cache_mounts: bool = False) -> list[str]:
    """Instructions that install the runtime's packages with its package manager.

    Without cache mounts the download cache is discarded (`--no-cache-dir` and
    friends), because anything left behind would sit in the image layer for good.
    With them the cache lives outside the image, so editing one package in a
    runtime re-downloads nothing — and keeping it in the layer would be the wrong
    trade.
    """
    packages = " ".join(validate_package_name(p) for p in runtime.packages)
    mounts = cache_mount_flags(runtime.package_manager) if cache_mounts else ""

    if runtime.package_manager == "pip":
        if cache_mounts:
            return [f"RUN {mounts} pip install {packages}"]
        return [f"RUN pip install --no-cache-dir {packages}"]

    if runtime.package_manager == "npm":
        # Installed into the work dir rather than globally, so application
        # libraries like react/react-dom resolve from user code.
        workdir = f"WORKDIR {shlex.quote(runtime.work_dir)}"
        if cache_mounts:
            return [workdir, f"RUN {mounts} npm install {packages}"]
        return [workdir, f"RUN npm install {packages}"]

    if runtime.package_manager == "apt":
        if cache_mounts:
            return [
                # Debian images ship a hook that deletes the archive cache after
                # every install, which would empty the mount we just gave apt.
                "RUN rm -f /etc/apt/apt.conf.d/docker-clean",
                f"RUN {mounts} apt-get update "
                f"&& apt-get install -y --no-install-recommends {packages}",
            ]
        return [f"RUN apt-get update && apt-get install -y {packages}"]

    if cache_mounts:
        return [f"RUN {mounts} cargo install {packages}"]
    return [f"RUN cargo install {packages}"]


def cache_mount_flags(package_manager: str) -> str:
    """`--mount` flags keeping one package manager's cache between builds.

    `sharing=locked` because two runtimes building at once would otherwise write
    the same cache directory concurrently.
    """
    targets = PACKAGE_CACHES[package_manager]
    return " ".join(f"--mount=type=cache,target={target},sharing=locked" for target in targets)


def validate_package_name(name: str) -> str:
    """Return `name` unchanged, or raise when it is not a package name.

    Raises:
        ValueError: If the name is empty or holds disallowed characters.
    """
    if not name or not PACKAGE_NAME.match(name):
        raise ValueError(f"Invalid package name: {name!r}")
    return name


def env_instruction(key: str, value: str) -> str:
    """Render one `ENV` instruction with the value quoted.

    Raises:
        ValueError: If the name is not a valid identifier or the value spans
            more than one line.
    """
    if not ENV_KEY.match(key):
        raise ValueError(f"Invalid environment variable name: {key!r}")
    if "\n" in value or "\r" in value:
        raise ValueError(f"Environment variable {key!r} value contains a newline")
    return f"ENV {key}={shlex.quote(value)}"


def reject_metacharacters(value: str, *, what: str) -> None:
    """Raise when `value` holds a shell metacharacter or newline.

    Args:
        value: The value about to be interpolated.
        what: Name of the setting, for the error message.

    Raises:
        ValueError: If any metacharacter is present.
    """
    found = SHELL_METACHARACTERS.intersection(value)
    if found:
        rendered = ", ".join(sorted(repr(c) for c in found))
        raise ValueError(f"{what} contains disallowed shell metacharacters: {rendered}")
