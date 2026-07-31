"""Hashline: content-hash-tagged line editing for AI agents.

Inspired by Can Bölük's hashline format. Each line is tagged with a 2-character
content hash. Models reference lines by `number:hash` pairs instead of
reproducing exact text, eliminating whitespace-matching errors and reducing
output tokens.

Format::

    1:a3|function hello() {
    2:f1|  return "world";
    3:0e|}

Usage::

    from pydantic_ai_backends.hashline import (
        line_hash,
        format_hashline_output,
        apply_hashline_edit,
    )

    # Format file content with hashline tags
    tagged = format_hashline_output(raw_content)

    # Apply an edit referencing lines by hash
    new_content, error = apply_hashline_edit(
        raw_content,
        start_line=2, start_hash="f1",
        new_content='  return "hello";',
    )
"""

from __future__ import annotations

import hashlib


def line_hash(content: str) -> str:
    """Generate a 2-char hex content hash for a line.

    Uses first 2 characters of MD5 hex digest.  Provides 256 possible
    values — sufficient for detecting changes within typical files.

    Args:
        content: The line content (without trailing newline).

    Returns:
        2-character lowercase hex string.
    """
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:2]


def _split_lines(content: str) -> tuple[list[str], bool]:
    """Split content into lines, tracking trailing newline.

    Returns:
        Tuple of (lines, has_trailing_newline).
    """
    has_trailing_newline = content.endswith("\n")
    lines = content.split("\n")
    # A trailing newline produces an extra empty string from split — remove it
    if has_trailing_newline and lines and lines[-1] == "":
        lines = lines[:-1]
    return lines, has_trailing_newline


def format_hashline_output(
    content: str,
    offset: int = 0,
    limit: int = 2000,
) -> str:
    """Format file content with hashline tags.

    Each line is prefixed with `{line_num}:{hash}|`.

    Args:
        content: Raw file content as a string.
        offset: 0-indexed line offset to start from.
        limit: Maximum number of lines to return.

    Returns:
        Formatted string with hashline-tagged lines.
    """
    lines, _ = _split_lines(content)
    total_lines = len(lines)

    if total_lines == 0:  # pragma: no cover
        return "(empty file)"

    if offset >= total_lines:
        return f"Error: Offset {offset} exceeds file length ({total_lines} lines)"

    end = min(offset + limit, total_lines)
    result_parts: list[str] = []

    for i in range(offset, end):
        line_num = i + 1  # 1-indexed
        h = line_hash(lines[i])
        result_parts.append(f"{line_num}:{h}|{lines[i]}")

    result = "\n".join(result_parts)

    if end < total_lines:
        result += f"\n\n... ({total_lines - end} more lines)"

    return result


def apply_hashline_edit(
    content: str,
    start_line: int,
    start_hash: str,
    new_content: str,
    end_line: int | None = None,
    end_hash: str | None = None,
    insert_after: bool = False,
) -> tuple[str, str | None]:
    """Apply a hashline edit, discarding the summary.

    See :func:`apply_hashline_edit_with_summary` for the arguments.

    Returns:
        `(new_file_content, error)`, where *error* is `None` on success.
    """
    result, error, _ = apply_hashline_edit_with_summary(
        content,
        start_line,
        start_hash,
        new_content,
        end_line,
        end_hash,
        insert_after,
    )
    return result, error


def apply_hashline_edit_with_summary(
    content: str,
    start_line: int,
    start_hash: str,
    new_content: str,
    end_line: int | None = None,
    end_hash: str | None = None,
    insert_after: bool = False,
) -> tuple[str, str | None, str]:
    """Apply a hashline edit to file content.

    The referenced line hashes are checked against the current content before
    anything changes. A mismatch rejects the edit — the file was modified since
    the model read it, so the line numbers it chose no longer mean what it thinks.

    Args:
        content: Current raw file content.
        start_line: 1-indexed line number to start the edit.
        start_hash: Expected 2-char hash of the start line.
        new_content: Replacement text; empty deletes the line(s).
        end_line: 1-indexed end of an inclusive range. `None` for single-line.
        end_hash: Expected 2-char hash of the end line.
        insert_after: Insert *after* `start_line` instead of replacing it.

    Returns:
        `(new_file_content, error, summary)`, where *error* is `None` on success.
    """
    lines, has_trailing_newline = _split_lines(content)
    total_lines = len(lines)

    # --- validate start line ---
    if start_line < 1 or start_line > total_lines:
        return content, (f"Line {start_line} out of range (file has {total_lines} lines)"), ""

    # insert_after inserts at a single point and ignores any range, so a caller
    # supplying end_line/end_hash would be silently misled into thinking a range
    # was honored. Reject the combination instead.
    if insert_after and (end_line is not None or end_hash is not None):
        return content, "end_line/end_hash cannot be combined with insert_after", ""

    actual_start_hash = line_hash(lines[start_line - 1])
    if actual_start_hash != start_hash:
        return (
            content,
            (
                f"Hash mismatch at line {start_line}: "
                f"expected '{start_hash}', got '{actual_start_hash}'. "
                "File may have changed — re-read it first."
            ),
            "",
        )

    # --- validate end line ---
    effective_end = start_line
    if end_line is not None:
        if end_line < start_line:
            return content, (f"end_line ({end_line}) must be >= start_line ({start_line})"), ""
        if end_line > total_lines:
            return content, (f"End line {end_line} out of range (file has {total_lines} lines)"), ""
        actual_end_hash = line_hash(lines[end_line - 1])
        if end_hash is not None and actual_end_hash != end_hash:
            return (
                content,
                (
                    f"Hash mismatch at line {end_line}: "
                    f"expected '{end_hash}', got '{actual_end_hash}'. "
                    "File may have changed — re-read it first."
                ),
                "",
            )
        effective_end = end_line

    # --- build new lines ---
    new_lines = new_content.split("\n") if new_content else []

    # --- apply ---
    if insert_after:
        result_lines = lines[:start_line] + new_lines + lines[start_line:]
    else:
        result_lines = lines[: start_line - 1] + new_lines + lines[effective_end:]

    result = "\n".join(result_lines)
    if has_trailing_newline:
        result += "\n"

    # --- summary ---
    lines_removed = effective_end - start_line + 1 if not insert_after else 0
    lines_added = len(new_lines)

    if insert_after:
        summary = f"Inserted {lines_added} line(s) after line {start_line}"
    elif not new_lines:
        summary = f"Deleted {lines_removed} line(s) at line {start_line}"
    elif lines_removed == lines_added:
        summary = f"Replaced {lines_removed} line(s) at line {start_line}"
    else:
        summary = (
            f"Replaced {lines_removed} line(s) with {lines_added} line(s) at line {start_line}"
        )

    return result, None, summary
