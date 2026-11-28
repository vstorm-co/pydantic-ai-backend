"""Standalone bounded PNG capture; executed inside the desktop's own container."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from contextlib import suppress
from typing import BinaryIO

INLINE_BYTES = 512 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 64 * 1024


def capture(path: str) -> dict[str, object]:
    """Stream small screenshots directly; spill a large frame without recapturing it."""
    process = subprocess.Popen(
        ["scrot", "--pointer", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    image = bytearray()
    output: BinaryIO | None = None
    published = False
    created = False
    total = 0
    try:
        if process.stdout is None:
            raise RuntimeError("The screen capture returned no output stream")
        while True:
            chunk = process.stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise RuntimeError("The screenshot exceeded its size limit")
            if output is None and total > INLINE_BYTES:
                descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                created = True
                output = os.fdopen(descriptor, "wb")
                output.write(image)
                image.clear()
            if output is None:
                image.extend(chunk)
            else:
                output.write(chunk)
        if process.wait() != 0:
            raise RuntimeError("The screen capture failed")
        if output is not None:
            output.close()
            output = None
            published = True
            return {"mode": "spill", "size": total}
        return {
            "mode": "inline",
            "size": total,
            "image_b64": base64.b64encode(image).decode("ascii"),
        }
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if output is not None:
            output.close()
        if created and not published:
            with suppress(FileNotFoundError):
                os.unlink(path)


def main() -> None:
    print(json.dumps(capture(sys.argv[1]), separators=(",", ":")))
