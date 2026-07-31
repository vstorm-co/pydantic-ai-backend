"""String replacement shared by every backend's `edit`.

Backends differ in how they fetch and store file content, but the replacement
itself — and the two ways it can be refused — are identical, so the rules and
their wording live here rather than in five copies.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Replacement:
    """Content after a successful replacement, and how many lines it touched."""

    content: str
    occurrences: int


def replace_in_content(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
) -> Replacement | str:
    """Replace `old_string` in `content`, or explain why it cannot be done.

    Returns:
        A :class:`Replacement`, or an error message when `old_string` is absent
        or ambiguous. The `list | str` shape mirrors `grep_raw`.
    """
    occurrences = content.count(old_string)

    if occurrences == 0:
        return f"String '{old_string}' not found in file"

    if occurrences > 1 and not replace_all:
        return (
            f"String '{old_string}' found {occurrences} times. "
            "Use replace_all=True to replace all, or provide more context."
        )

    if replace_all:
        return Replacement(content.replace(old_string, new_string), occurrences)
    return Replacement(content.replace(old_string, new_string, 1), 1)
