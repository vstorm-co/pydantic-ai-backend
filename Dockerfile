# The sandbox service. This is the process that holds the Docker socket, and
# nothing else in a deployment should.
#
# Built from the source tree rather than from PyPI so the image and the release
# are the same code, with no window where one is published and the other is not.
# Installed as a package rather than copied into place, so what runs here is the
# wheel users get.

FROM python:3.14-slim AS build

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# A virtualenv rather than the system site-packages: it is one directory to copy
# into the final stage, which leaves hatchling, pip and the build cache behind.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir '.[server]'


FROM python:3.14-slim

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SANDBOXD_HOST=0.0.0.0 \
    SANDBOXD_PORT=8080 \
    SANDBOXD_WORKSPACE_ROOT=/workspaces

# Unprivileged, and the workspace root owned by that user so a named volume
# mounted there inherits the ownership on first use.
#
# This does not stop the process being host-root-equivalent — anything that can
# reach the Docker socket can start a privileged container that mounts `/`. What
# it bounds is a bug in *this* service: a path that escapes a workspace writes as
# uid 10001 rather than as root. The socket is reached by supplementary group,
# not by running as root:
#
#     group_add: ["${DOCKER_GID}"]      # stat -c '%g' /var/run/docker.sock
#
# Without that the daemon is unreachable and every session fails to start, which
# is the one failure to expect from this image.
RUN useradd --system --uid 10001 --create-home --home-dir /home/sandboxd sandboxd \
 && mkdir -p /workspaces \
 && chown sandboxd:sandboxd /workspaces

USER sandboxd
WORKDIR /home/sandboxd
EXPOSE 8080

# Reads the port from the environment rather than repeating it: an operator who
# moves the service would otherwise get a container that is permanently
# unhealthy while serving perfectly.
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import os, sys, urllib.request; \
port = os.environ.get('SANDBOXD_PORT', '8080'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz').status == 200 else 1)"

# Configured entirely by SANDBOXD_* variables; `SANDBOXD_TOKEN` is required and
# the service exits naming it when it is absent.
CMD ["python", "-m", "pydantic_ai_backends.remote.server"]
