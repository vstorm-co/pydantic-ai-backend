"""Import every public name of the package, and fail naming each one that does not.

Run in an environment holding only the package and the extras under test: every
other job installs all of them, so a module importing a package that no extra it
ships with declares passes there and fails for anyone who installs just that
extra.
"""

from __future__ import annotations

import sys

import pydantic_ai_backends


def main() -> int:
    names = pydantic_ai_backends.__all__
    failed: list[str] = []
    for name in names:
        try:
            getattr(pydantic_ai_backends, name)
        except ImportError as exc:
            failed.append(f"{name}: {exc}")
    for line in failed:
        print(line, file=sys.stderr)
    print(f"{len(names) - len(failed)} of {len(names)} names import")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
