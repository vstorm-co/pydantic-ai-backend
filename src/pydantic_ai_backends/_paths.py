"""Path handling for backends that address files by virtual POSIX path."""

from __future__ import annotations


def normalize_path(path: str) -> str:
    """Return `path` as an absolute POSIX path without a trailing slash."""
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path


def unsafe_path_reason(path: str) -> str | None:
    """Return why `path` is unsafe as a virtual path, or `None` when it is fine.

    Virtual paths never reach a real filesystem, so the checks reject the forms
    that would make one ambiguous rather than the ones an OS would refuse.
    """
    if ".." in path:
        return "Path cannot contain '..'"
    if path.startswith("~"):
        return "Path cannot start with '~'"
    if len(path) > 1 and path[1] == ":":
        return "Windows absolute paths are not allowed"
    return None
