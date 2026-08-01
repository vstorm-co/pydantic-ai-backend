"""Ready-made runtime environments for `DockerSandbox`.

Two kinds of entry, and the difference is what the first use costs:

- **Ready-made images** name an `image` and start in the time it takes to pull
  one. Nothing is built.
- **Built runtimes** name a `base_image` plus `packages`, so the first use builds
  an image and later uses hit the cache. Worth it when the packages are large
  enough that installing them per session would dominate.

On a memory-constrained host the choice of *library* moves capacity further than
any container tuning does. Grouping a 188 MB CSV, measured: pandas peaks at
570 MB and is killed outright under a 384 MB ceiling, where DuckDB does the same
work in 312 MB and Polars survives the ceiling too — both roughly eight times
faster. `python-analytics` is the runtime to reach for when sandboxes are small;
`python-datascience` exists because most model-written code reaches for pandas,
and its ceiling has to be sized for pandas' appetite rather than the task's.

Example:
    ```python
    from pydantic_ai_backends import BUILTIN_RUNTIMES, DockerSandbox

    sandbox = DockerSandbox(runtime="python-datascience")
    sandbox = DockerSandbox(runtime=BUILTIN_RUNTIMES["node-react"])
    ```
"""

from __future__ import annotations

from pydantic_ai_backends.types import RuntimeConfig

NODE_WORK_DIR = "/app"
"""Node tooling resolves `node_modules` from the working directory, so the
JavaScript runtimes install into the directory they also run in."""

CODING_TOOLS = "git curl ca-certificates ripgrep fd-find jq less procps"
"""What an agent needs to *look* at a codebase, and what each part costs.

Measured over `python:3.12-slim` (41.5 MB): `git` is 33.1 MB and unavoidable,
and `ripgrep`, `fd-find`, `jq`, `less` and `procps` come to **4.3 MB between
them**. Leaving those out to save four megabytes is a bad trade at any image
size — without `rg` an agent falls back to `grep -r`, which floods its own
context with matches from `node_modules` and `.git`, and without `procps` it has
no `ps` or `kill`, so backgrounding a server is a one-way door.

Deliberately absent is `build-essential`: 94 MB, more than doubling a minimal
runtime, to compile wheels that manylinux already ships built.
"""

BUILTIN_RUNTIMES: dict[str, RuntimeConfig] = {
    "coding": RuntimeConfig(
        name="coding",
        description="Python with git, ripgrep, fd, jq and uv — the runtime for working on code",
        base_image="python:3.12-slim",
        setup_commands=[
            f"apt-get update && apt-get install -y --no-install-recommends {CODING_TOOLS} "
            # Debian ships the binary as `fdfind` because of a name clash, and a
            # model that has read fd's documentation types `fd`.
            "&& ln -s /usr/bin/fdfind /usr/local/bin/fd "
            "&& rm -rf /var/lib/apt/lists/*",
            # A single binary rather than `pip install uv`: 19.9 MB against
            # 23.8 MB, and nothing lands in site-packages for user code to
            # collide with. Second, because the installer needs the curl the
            # line above provides.
            "curl -fsSL https://astral.sh/uv/install.sh "
            "| env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh",
        ],
        # No libraries: what an agent needs here is tools, and whatever the
        # project it is working on declares. `uv` is what makes installing those
        # cheap — measured at 5-7x pip on the same package set.
    ),
    "polyglot": RuntimeConfig(
        name="polyglot",
        description="Python and Node together, with curl and git — installs more on demand",
        base_image="python:3.12-slim",
        setup_commands=[
            # One layer: apt's lists are large and there is no reason to keep them
            # in the image once the install is done.
            "apt-get update && apt-get install -y --no-install-recommends "
            "curl ca-certificates git nodejs npm && rm -rf /var/lib/apt/lists/*",
            # Debian's Node is current (20.19.2 against node:20-slim's 20.20.2),
            # but its npm is a major version behind at 9.2.0. Upgrading only npm
            # costs a few megabytes and avoids a runtime that quietly behaves
            # differently from the dedicated Node ones.
            "npm install -g npm@10 && npm cache clean --force",
        ],
        # Deliberately without pandas: at 97 MB of import it would take a third of
        # a 256 MB sandbox before doing any work, and DuckDB and Polars answer the
        # same questions in less. `python-datascience` is there when pandas is
        # what the code wants.
        packages=["numpy", "duckdb", "polars", "httpx"],
    ),
    "python-minimal": RuntimeConfig(
        name="python-minimal",
        description="Python 3.12, standard library only",
        image="python:3.12-slim",
    ),
    "python-datascience": RuntimeConfig(
        name="python-datascience",
        description="pandas, numpy, matplotlib, scikit-learn, seaborn — needs room",
        base_image="python:3.12-slim",
        packages=["pandas", "numpy", "matplotlib", "scikit-learn", "seaborn"],
    ),
    "python-analytics": RuntimeConfig(
        name="python-analytics",
        description="DuckDB and Polars — same work in a third of the memory",
        base_image="python:3.12-slim",
        packages=["duckdb", "polars", "pyarrow"],
    ),
    "python-web": RuntimeConfig(
        name="python-web",
        description="FastAPI, Uvicorn, SQLAlchemy, httpx",
        base_image="python:3.12-slim",
        packages=["fastapi", "uvicorn", "sqlalchemy", "httpx"],
    ),
    "python-scraping": RuntimeConfig(
        name="python-scraping",
        description="httpx, BeautifulSoup, lxml and markdownify for fetching pages",
        base_image="python:3.12-slim",
        packages=["httpx", "beautifulsoup4", "lxml", "markdownify"],
    ),
    "python-documents": RuntimeConfig(
        name="python-documents",
        description="pypdf, python-docx, openpyxl and Pillow for document work",
        base_image="python:3.12-slim",
        packages=["pypdf", "python-docx", "openpyxl", "pillow"],
    ),
    "node-minimal": RuntimeConfig(
        name="node-minimal",
        description="Node.js 20, no extra packages",
        image="node:20-slim",
        work_dir=NODE_WORK_DIR,
    ),
    "node-typescript": RuntimeConfig(
        name="node-typescript",
        description="TypeScript with tsx and Vitest",
        base_image="node:20-slim",
        packages=["typescript", "tsx", "vitest"],
        package_manager="npm",
        work_dir=NODE_WORK_DIR,
    ),
    "node-react": RuntimeConfig(
        name="node-react",
        description="TypeScript, Vite and React",
        base_image="node:20-slim",
        packages=["typescript", "vite", "react", "react-dom", "@types/react"],
        package_manager="npm",
        work_dir=NODE_WORK_DIR,
    ),
    "bun": RuntimeConfig(
        name="bun",
        description="Bun, with its own bundler, test runner and package manager",
        image="oven/bun:1-slim",
        work_dir=NODE_WORK_DIR,
    ),
    "deno": RuntimeConfig(
        name="deno",
        description="Deno, TypeScript-first with no install step",
        image="denoland/deno:alpine",
        work_dir=NODE_WORK_DIR,
    ),
    "go": RuntimeConfig(
        name="go",
        description="Go toolchain",
        image="golang:1.23-alpine",
        work_dir="/src",
    ),
    "rust": RuntimeConfig(
        name="rust",
        description="Rust toolchain with cargo",
        image="rust:1-slim",
        work_dir="/src",
    ),
}
"""Runtimes available by name. Keys are stable: a stored configuration names one."""


def get_runtime(name: str) -> RuntimeConfig:
    """Look up a built-in runtime.

    Args:
        name: Runtime name, e.g. `"python-datascience"`.

    Raises:
        KeyError: If no built-in runtime has that name.
    """
    if name not in BUILTIN_RUNTIMES:
        available = ", ".join(sorted(BUILTIN_RUNTIMES))
        raise KeyError(f"Unknown runtime '{name}'. Available: {available}")
    return BUILTIN_RUNTIMES[name]
