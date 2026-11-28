"""Build only local benchmark runtime images from the installed candidate wheel."""

from __future__ import annotations

import argparse
import json
import signal
import time
from contextlib import closing
from pathlib import Path

import docker

from pydantic_ai_backends.backends.docker._image import build_image
from pydantic_ai_backends.backends.docker.runtimes import get_runtime


def expired(_signum: int, _frame: object) -> None:
    raise TimeoutError("Bounded image build exceeded 15 minutes")


def main() -> None:
    signal.signal(signal.SIGALRM, expired)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report: dict[str, object] = {"images": {}, "started_monotonic": time.monotonic()}
    path = args.output
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(docker.from_env(timeout=900)) as client:
        info = client.info()
        report["daemon"] = {
            key: info.get(key)
            for key in ["ID", "Name", "Architecture", "OSType", "NCPU", "MemTotal", "ServerVersion"]
        }
        for alias in ("browser", "desktop"):
            signal.alarm(900)
            started = time.monotonic()
            # Keep benchmark image pruning separate from an operator's runtime tags.
            runtime = get_runtime(alias).model_copy(update={"name": "benchmark-" + alias})
            print("BUILD_START " + alias, flush=True)
            image = build_image(client, runtime)
            signal.alarm(0)
            actual = client.images.get(image)
            images = report["images"]
            assert isinstance(images, dict)
            images[alias] = {
                "tag": image,
                "id": actual.id,
                "size_bytes": actual.attrs["Size"],
                "elapsed_seconds": time.monotonic() - started,
                "scope": "First build on current Docker daemon; shared base/layers may be cached",
            }
            path.write_text(json.dumps(report, indent=2))
            print("BUILD_DONE " + json.dumps({alias: images[alias]}), flush=True)
    path.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
