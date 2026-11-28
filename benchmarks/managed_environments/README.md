# Managed environment acceptance and local benchmarks

These opt-in scripts exercise an **installed local wheel**, Docker containers and
native GUI operations without an agent or model call. They are outside pytest's
default suite. Run on Linux or macOS with a local Docker daemon; the workspace
path must be shared with the daemon. `ps` provides optional server-process CPU/RSS
samples. Containers have one CPU, 1 GiB memory and no network for the baseline.
The harness creates at most two of its own runtime containers simultaneously and
removes only its random session names. It does not stop other containers.

Use a quiet machine for comparisons. The scripts take screenshots and deliberately
terminate their own test browser. Output, credentials and synthetic screenshots
belong in a fresh temporary folder, never in this repository. The service is bound
to loopback; no Chromium debugger port is published.

## Build and install the wheel

Run these blocks in the same Bash session, starting at the repository root:

```bash
set -euo pipefail
BENCH_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pab-benchmark.XXXXXX")"
chmod 700 "$BENCH_ROOT"
uv build --wheel --out-dir "$BENCH_ROOT/wheels"
BENCH_WHEEL="$(find "$BENCH_ROOT/wheels" -name '*.whl' -print -quit)"
uv venv --python 3.12 "$BENCH_ROOT/venv"
BENCH_PY="$BENCH_ROOT/venv/bin/python"
uv pip install --python "$BENCH_PY" \
  "${BENCH_WHEEL}[server,browser,desktop,workspaces,docker]"
```

The local feature wheel retains the repository's existing version metadata until
a release is authorized. Its SHA-256 and installed import path identify the tested
artifact. Installing a package of the same version from PyPI does not reproduce
these feature results. `benchmark.py` refuses an editable/source import.

## Prepare isolated runtime images

```bash
env -u PYTHONPATH "$BENCH_PY" benchmarks/managed_environments/build_images.py \
  --output "$BENCH_ROOT/image-builds.json"
BENCH_BROWSER_IMAGE="$("$BENCH_PY" -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["images"]["browser"]["tag"])' \
  "$BENCH_ROOT/image-builds.json")"
BENCH_DESKTOP_IMAGE="$("$BENCH_PY" -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["images"]["desktop"]["tag"])' \
  "$BENCH_ROOT/image-builds.json")"
```

The helper copies the built-in recipes under `benchmark-browser` and
`benchmark-desktop` image names, keeping image pruning separate from an operator's
runtime aliases. Each build has a 15-minute outer deadline. Images remain cached
for repeat runs. A first build may share existing base/layers; it is not a
cache-cleared cold-build measurement. The helper uses the repository's internal
image builder, so it travels with this source rather than promising a stable SDK
entry point.

## Run the offline acceptance and benchmark

Choose an unused local port and start the service from the installed wheel:

```bash
BENCH_RUN="$BENCH_ROOT/run"
BENCH_PORT="$("$BENCH_PY" -c \
  'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
env -u PYTHONPATH "$BENCH_PY" benchmarks/managed_environments/server.py \
  --folder "$BENCH_RUN" --port "$BENCH_PORT" \
  --browser-image "$BENCH_BROWSER_IMAGE" --desktop-image "$BENCH_DESKTOP_IMAGE" \
  > "$BENCH_ROOT/server.log" 2>&1 &
BENCH_SERVER_PID=$!
trap 'kill -INT "$BENCH_SERVER_PID" 2>/dev/null || true' EXIT
# Wait until this own service is ready before invoking the benchmark.
"$BENCH_PY" -c \
  'import sys,time,urllib.request; time.sleep(1); urllib.request.urlopen(sys.argv[1]+"/healthz",timeout=5).read()' \
  "http://127.0.0.1:$BENCH_PORT"
env -u PYTHONPATH "$BENCH_PY" benchmarks/managed_environments/benchmark.py \
  --folder "$BENCH_RUN" --base-url "http://127.0.0.1:$BENCH_PORT" \
  --wheel "$BENCH_WHEEL" --samples 25
kill -INT "$BENCH_SERVER_PID"
wait "$BENCH_SERVER_PID"
trap - EXIT
```

The server refuses to overwrite an existing service credential: use a new output
folder per server. A port race or startup failure should fail the command; inspect
the service log rather than attaching to a different service. The example is for
a shell script (`EXIT` trap); keep the service running manually only when another
explicit test needs it.

The harness checks:

- Exact DOM, unsaved input and JavaScript state after a fresh client process
  reconnects; repeated reconnect does not accumulate bridge processes.
- Two-container/context isolation, session-token refusal, native Enter submission,
  CPU/memory limits, no published ports and a killed container without healing.
- Passive frames do not renew idle, physical input does, and idle/hard deadlines
  revoke access before asynchronous cleanup stops the container.
- A real Desktop terminal retains an unsent command across adapter detach; frame
  temporary files are removed and workspace files survive container retirement.
- Latency distributions from 25 sequential samples, pooled/unpooled Desktop
  capture, HTTP request counts, and short steady-stream resource samples.

Browser pages are synthetic `about:blank` content. The mandatory proxy is an
unreachable loopback endpoint and the container has `network=none`; both private
and public navigation must fail. This baseline does **not** establish working
public proxy connectivity. `--desktop-only` or `--skip-browser` selects a smaller
acceptance scope and produces a separately named receipt.

## Optional real proxy-policy acceptance

This separate test creates an internal bridge, one Browser and one Squid proxy.
Only the proxy also joins an outbound bridge. It makes one public HTTPS page load
and checks denials for metadata, private addressing and the browser's own loopback
CDP endpoint. It is capability acceptance, not an Internet latency benchmark.

Provide the actual deployment's reviewed Squid configuration; the baseline used
[AgenticOS's configuration](https://github.com/vstorm-co/agenticos/blob/be8d094de00264d53323834db7996a8050fa194d/docker/browser-egress/squid.conf).
Do not substitute a more permissive policy and describe it as the same test.

```bash
docker pull ubuntu/squid:6.6-24.04_edge
env -u PYTHONPATH "$BENCH_PY" benchmarks/managed_environments/proxy_acceptance.py \
  --folder "$BENCH_ROOT/proxy-run" --wheel "$BENCH_WHEEL" \
  --browser-image "$BENCH_BROWSER_IMAGE" --desktop-image "$BENCH_DESKTOP_IMAGE" \
  --proxy-config /path/to/deployment/squid.conf
```

This test starts/stops its own loopback service and removes its own proxy,
Browser and network. Do not run it concurrently with another benchmark: two
additional containers and external network variability would distort measurements.
The policy hash, proxy image ID and separate public/denial outcomes are recorded.

## Recorded candidate03 baseline — 2026-11-28

The sanitized JSON files under `results/` preserve a **baseline before the
single-command Desktop capture optimization**. They are evidence for that wheel,
not a claim that today's source has already been measured. The local wheel was
`pydantic_ai_backend-0.2.42-py3-none-any.whl`, SHA-256
`fd74920ab328b753ea1ceb9988166da82220d7648f07460f1d0ff837e8679d54`.

Docker Desktop 29.2.1 ran Linux ARM64 with 10 virtual CPUs and 7.75 GiB allocated;
each runtime was limited to one CPU / 1 GiB. The Browser image was 340 MB
(`49e80f75…`), the minimal Desktop image 351 MB (`76466a8b…`); full IDs are in the
image-build receipt. Images built in 49.8 s / 57.8 s with possible shared base/layer
cache. Fresh container startup below uses those cached images.

| Measurement | Median | p95 | Scope |
| --- | ---: | ---: | --- |
| Browser screenshot | 35.4 ms | 51.6 ms | 25 samples, light synthetic page |
| Browser single-character input | 2.65 ms | 4.25 ms | 25 samples |
| Browser exact-context reconnect | 92.6 ms | 107.2 ms | 25 samples |
| Desktop screenshot, unpooled HTTP | 318.5 ms | 349.0 ms | 25 samples |
| Desktop screenshot, pooled HTTP | 237.6 ms | 263.1 ms | 25 samples, **7 RPCs per frame** |
| Desktop key chord | 77.8 ms | 83.8 ms | 25 samples |

Fresh cached-image container startup took 201 ms for Browser and 115 ms for
Desktop; initial GUI startup took 1142 ms and 861 ms respectively. These are single
observations, not startup distributions. The seven Desktop requests were five
`/run`, one `/files/read` and one `/files/remove`: the temporary-file paths caused
native-file fallbacks. Keep this baseline separate from a later optimized run.

| Short stream | Achieved FPS | Median working set | Median container CPU |
| --- | ---: | ---: | ---: |
| Browser idle | — | 293.6 MiB | 1.49% |
| Browser requested 1 FPS | 1.0 | 287.0 MiB | 3.11% |
| Browser requested 10 FPS | 9.9 | 292.0 MiB | 24.25% |
| Desktop requested 1 FPS | 1.0 | 101.3 MiB | 7.05% |
| Desktop requested 5 FPS | 3.12 | 101.5 MiB | 17.18% |

CPU is expressed relative to **one core**; 100% means one core, not all ten VM
cores. Working set excludes inactive file cache; it is not process RSS or image
size. Each stream lasted six seconds and yielded only three Docker stats samples;
server RSS/CPU are separate raw fields. Host load, initial caches, renderer/page
complexity and network latency were not controlled as in a laboratory benchmark.
These measurements locate bottlenecks, not production capacity guarantees.

All 15 offline acceptance checks and the separate production-proxy acceptance
passed for candidate03. The receipts exclude credentials, private absolute paths,
raw screenshots and daemon identifiers. Their synthetic session names and
monotonic sample clocks are normalized; raw timings and resource values remain.

## Candidate04 acceptance and optimized capture — 2026-11-28

The installed local candidate04 wheel SHA-256 is
`cd646a404796e64eb19b41194a7e668205e788c20ad821d6cb37281b66d912ad`.
All 15 acceptance checks passed again with 25 latency samples. Browser screenshot
median/p95 was 34.1/53.7 ms; Desktop capture was 83.5/97.7 ms with unpooled HTTP
and 72.5/124.2 ms with pooled HTTP. The pooled trace recorded **25 requests for
25 captures: one `/run` RPC per frame**. The separately measured same-session
candidate03/candidate04 comparison should be used to attribute speed differences;
these complete acceptance runs also include different runtime recipes and timing.

On the same Docker allocation, six-second streams again yielded only three stats
samples: Browser requested 10 FPS achieved 9.91 FPS, median 296.6 MiB working set /
23.5% of one CPU core; Desktop requested 5 FPS achieved 4.97 FPS, 99.0 MiB / 23.5%.
These short synthetic measurements remain diagnostic, not capacity guarantees.
The watchdog test started after the final resource sample had completed.

The new Browser runtime image is
`sha256:ebb696d71b8248a474cec68f5ede1db2d7c6e73f30a297b249c266f75455f4fd`;
`websocket-client` 1.9.2 was verified inside the actual container. The recipe build
took 4.0 seconds with cached shared layers, separately from runtime startup.

`watchdog_acceptance.py` starts its own service, creates one Browser with an
eight-second lifetime, reconnects without changing its original deadline, and
**SIGKILLs its own sandboxd process**. Candidate04's container exited 29 ms after
the original deadline, 6.375 seconds after the service was killed. This verifies
an independent container deadline after supervisor death, beyond client detach.
The fixture removes only its exact test container and private service credential.

```bash
env -u PYTHONPATH "$BENCH_PY" benchmarks/managed_environments/watchdog_acceptance.py \
  --folder "$BENCH_ROOT/watchdog-run" --wheel "$BENCH_WHEEL" \
  --browser-image "$BENCH_BROWSER_IMAGE" --desktop-image "$BENCH_DESKTOP_IMAGE"
```

The separate candidate04 production-Squid acceptance also passed: public HTTPS,
three private/loopback denials and transport reconnect under the same policy hash.
See `results/candidate04-proxy-acceptance.json`; all of its own containers and
network were removed after the test.

## Candidate05 compatibility follow-up

Candidate05 fixes Python 3.10 timeout classification and replaces the client's
`asyncio.wait_for` send/response pair with one AnyIO deadline, preserving caller
cancellation on older interpreters. Gateway and transport regressions passed on
actual Python 3.10 and 3.11 (75 tests each). This wheel has SHA-256
`d0f55286f398ae3cc721f3bb0bdb4c587ffb0462f7150f99ea5f29ea5f0901ef`.

All 15 installed-wheel acceptance checks passed again with 25 latency samples,
along with separate production-proxy and supervisor-SIGKILL acceptance. Browser
capture median/p95 was 34.77/52.98 ms; pooled Desktop capture was 70.77/82.30 ms,
with 25 RPCs for 25 captures. The unchanged original Browser watchdog deadline
stopped its container 38.5 ms after expiry. Runtime recipes are unchanged from
candidate04, whose cached images were reused. The candidate04 interleaved A/B
remains the controlled comparison for the capture optimization; this repeat is
acceptance of the final timeout/cancellation changes.

## Final explicit-guard refactor smoke — candidate06

After the behavior-preserving cleanup/explicit-guard changes for Python 3.10
coverage, the installed candidate06 wheel passed all 15 native acceptance checks
again. Its SHA-256 is
`9da4ee7d41e5bb103884055dede9ccf62081aac345ec0ac32780551528889a3e`.
This bounded smoke used five repetitions and skipped resource sampling and timed
stream profiling; it makes no new performance claim. Candidate05's full 25-sample,
production-proxy and independent-watchdog receipts remain the detailed evidence.
See `results/candidate06-smoke.json`. No package release was published.

## Persistent-profile and Desktop HOME acceptance

Run `persistence_acceptance.py` with an installed candidate that includes persistent
profiles. It starts its own service with **explicit** `persist_browser_profiles`
and `persist_desktop_home` opt-ins and a private storage root. The custom benchmark
Desktop image runs as root, so the fixture explicitly declares `/root` as its HOME;
operators must match their actual image UID and HOME instead.

```bash
env -u PYTHONPATH "$BENCH_PY" benchmarks/managed_environments/persistence_acceptance.py \
  --folder "$BENCH_ROOT/persistence-run" --wheel "$BENCH_WHEEL" \
  --browser-image "$BENCH_BROWSER_IMAGE" --desktop-image "$BENCH_DESKTOP_IMAGE"
```

The fixture adds one container serving a local HTTPS origin and an HTTP CONNECT
proxy that permits **only** that exact origin. It has no external network. A
certificate bypass is enabled only on the disposable test Browser; this validates
HTTPS cookie/storage behavior, not production certificate trust. The production
Squid policy remains a separate acceptance test.

The checks validate server-side persistent and session-only `Secure; HttpOnly`
cookies, localStorage, committed IndexedDB, fast logout followed immediately by
suspend, actual container removal/recreation and explicit resume. Old live
context/target IDs are refused after a new generation starts. Wrong keys, corrupt
ciphertext, simultaneous starts, tenant/session isolation, ordinary End versus
purge, storage-owner locking and Desktop HOME/config/workspace files are also
checked. User/agent/conversation ownership mapping belongs to application tests.
No cookie values, profile keys or raw profiles belong in the recorded receipts.

The recorded findings preserve the failed experiments: candidate07 exposed Docker
archive APIs missing live tmpfs contents; candidate08 exposed logout flush timing;
candidate09 passed all ten functional checks, but a remaining native socket-reader
thread prevented orderly service exit. Its receipt remains failed, with that
cleanup limitation explicit. A later passing receipt must identify its own wheel.

The watchdog fixture also accepts `--stop-chromium-before-service-kill`. It stops
only the exact recorded Chromium process group, verifies Docker is **not** paused
and the independent watchdog remains runnable, then kills its own service. This
simulates the process state of an interrupted checkpoint; it does not claim that
a real periodic archive operation overlapped the kill. Candidate09's container
stopped within the recorded 0.25-second deadline-observation tolerance.

## Final combined persistence acceptance — candidate14

Installed local wheel SHA-256
`6d3831ce9e7f283870c18261d9480ae56f1f171a7226bd9e1cf6e8c5b5940eff`
passed all **ten** combined persistence/ownership checks with a three-second
sandbox idle timeout. Both persistent and session-only HTTPS cookies, localStorage
and IndexedDB survived actual container replacement; immediate logout remained
logged out. Desktop HOME/config/workspace files survived idle and replacement.
The caller owns a pooled HTTP client for Desktop operations, keeping connection
setup outside the short idle budget. A dormant session token may expire; orphan
profile purge deliberately uses the trusted application credential and starts no
runtime. This is distinct from silently renewing a stale live session.

The service exited with code **0 in 0.614 seconds**. Metadata-only tracing found no
active channels/sandboxes or remaining native reader frames at thread shutdown.
The receipt is `results/candidate14-persistence-acceptance.json`. Earlier failed
receipts remain as evidence of the tmpfs, flush and cancellation defects the
iterations found. The normal 25-sample performance results remain separate;
this functional run makes no new latency or production-capacity claim.

The local preview was then upgraded to this wheel without publishing it. Existing
private profile ciphertext, Vault data and subscription accounts were retained.
