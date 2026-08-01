"""Path handling for backends that address files by virtual POSIX path."""

from __future__ import annotations


def normalize_path(path: str) -> str:
    """Return `path` as an absolute POSIX path without a trailing slash.

    `.` segments are dropped, so the three ways a caller spells a backend's root
    — `"/"`, `""` and `"."` — all arrive as `"/"`. That matters because the
    console toolset's `ls` and `glob` default to `"."`: prefixing a slash without
    resolving it produced `"/."`, a directory no file is ever stored under, so a
    virtual-path backend answered every default listing with nothing at all.

    `..` is *not* resolved here — :func:`unsafe_path_reason` rejects it outright,
    and resolving it in this function would quietly turn a path the caller should
    have been told about into a valid one.
    """
    segments = [segment for segment in path.split("/") if segment and segment != "."]
    return "/" + "/".join(segments) if segments else "/"


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
