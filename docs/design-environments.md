# Managed browser and desktop environments

Implementation work in progress, 2026-11-28.

Problem: Browser and Desktop runtimes in AgenticOS duplicate reusable infrastructure and built-in Browser shares one Chromium process. Move low-level browser/desktop APIs and managed session lifecycle to pydantic-ai-backend/sandboxd; retain model tools, product authorization and UI in AgenticOS.

Acceptance: typed Python 3.10+ APIs, optional dependencies; exact live identities; sandboxd ownership survives application disconnect/restart; no public container/CDP ports; tenant-scoped authorization; passive observation cannot extend idle; separate hard lifetime; fencing for input; idempotent durable-visible cleanup; temporary provider failure differs from confirmed gone; real local browser/desktop isolation and restart tests without model calls; strict checks and coverage; multiple review rounds before any eventual release.

Scope: library browser/desktop packages, sandboxd lifecycle/gateway/runtime, docs/examples/tests, then AgenticOS adapters/runtime configuration. Preserve existing storage/workspace API and external/local providers.

Validation sequence: build a local wheel, install it into isolated library/service and AgenticOS environments, exercise actual containers and record latency/resource/failure measurements, then refine through independent review. Publication, release tags and production rollout are deferred by the maintainer. Both PRs stay reviewable without claiming an unpublished package version exists.
