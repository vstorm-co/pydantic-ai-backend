# Project filesystem exporter

A private NFSv3-to-HTTP adapter for live project files. It contains no project
file-data or stat cache and no local project replica. Each NFS operation reaches
the authenticated gateway. Successful WRITE replies declare FILE_SYNC only after
the HTTP mutation reply; the gateway must commit its transaction and provider
flush before returning that reply. Unknown acceptance is never retried by this
adapter.

The export root is the binding's authorized live lease directory list. Paths are
immutable lease UUID prefixes followed by relative project paths. The gateway,
not NFS AUTH_SYS IDs or file modes, is responsible for current membership,
audience, lease lifetime, project generation and write authorization on every
operation. Run this helper on a broker-private Docker bridge with no published
ports; agents receive a daemon-mounted named NFS volume, without mount privileges
or access to the helper's session token. Linux client caching must be disabled
(`noac,actimeo=0,lookupcache=none,sync`).

## Configuration

- `PROJECTFS_RPC_URL`: exact trusted binding filesystem endpoint, without query,
  fragment or embedded credentials.
- `PROJECTFS_TOKEN`: binding bearer credential, passed by the trusted broker.
- `PROJECTFS_UID` / `PROJECTFS_GID`: presented ownership, both default to `1000`.
- `PROJECTFS_READ_ONLY=1`: refuse mutations before sending an HTTP request.
- `PROJECTFS_LISTEN`: default `0.0.0.0:2049`; no host port publication.

The image runs as a non-root user. Its healthcheck sends a bounded NFSv3 NULL RPC
to loopback; a mere open TCP socket is not considered a successful healthcheck.
HTTP calls have a 15-second total deadline and a 2 MiB response limit. NFS reads
and writes are capped at 128 KiB. HTTP redirects are refused, response shapes are
strictly validated, and transport failures return I/O errors. Auth refusal returns
access denied. Session tokens, request payloads and remote error messages are not
logged.

## Protocol and lifecycle limits

`nfsembed` is pinned to `0.3.0`. Its CREATE implementation preserves EXCLUSIVE
verifiers, validates WRITE stability and propagates SETATTR failures. EXCLUSIVE
create requires atomic durable gateway replay of its eight-byte verifier. A
provider without that ability must return ENOTSUP, rather than pretend to honor
exclusive retries. SETATTR size, mode, mtime and guard are one gateway mutation;
a failed mtime guard uses the explicit protocol error number `10002` (NOT_SYNC).

Only a bounded, metadata-only object-handle-to-path index is held in memory.
Rename through this helper updates descendants and preserves their handles;
delete invalidates them. A missing known path returns STALE. External rename,
delete/recreate and concurrent changes outside the gateway cannot provide full
POSIX inode identity using a path-only upstream contract. No such identity is
claimed here. Symlinks, hard links, device nodes, arbitrary ownership changes and
a distinct atime are explicitly unsupported. Quota/statfs information is not
invented: FSSTAT remains unsupported until the gateway supplies a contract.

**Restart requires remount.** The library creates a fresh authenticated handle
codec on every helper start and does not expose a public restore API. Persisting
our path map would not preserve kernel handles. The broker must detect a changed
helper epoch, stop affected agent processes, unmount/remount the same project
volume and restart the same existing agent container, preserving its own
workspace. Interrupted commands fail and must never be automatically replayed.
If recovery cannot be guaranteed, report workspace unavailable; never create an
empty replacement workspace. The helper is not safe to silently reconnect under
an existing mounted client.

## Verification

```sh
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo fmt --check
docker build -t agenticos-projectfs:local .
```

The scoped tests use a synthetic HTTP gateway to verify current reads and lease
revocation, synchronous write acknowledgement, strict protocol validation,
EXCLUSIVE verifiers, guarded SETATTR, rename handles, read-only refusal and
unsupported operations. A separate Linux-kernel mount proof must pass before
product integration, including temporary-file O_EXCL, binary write/fsync,
rename/truncate, outside changes and helper restart followed by explicit remount.

The Linux-kernel proof passed with a synthetic gateway implementing durable
EXCLUSIVE verifiers. A separate proof returning ENOTSUP for EXCLUSIVE did not
produce a usable Linux fallback: `tempfile` failed with an I/O error. Such
providers must not be admitted as live project mounts until they implement the
required durable create contract. This test is transport/kernel evidence, not a
claim that application authorization or every storage provider has been tested.
