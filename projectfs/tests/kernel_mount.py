import argparse
import base64
import errno
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

parser = argparse.ArgumentParser(
    description="Synthetic Linux-kernel NFS adapter proof; requires a local Docker daemon"
)
parser.add_argument("--image", required=True)
parser.add_argument("--client-image")
parser.add_argument("--exporter-entrypoint", default="/usr/local/bin/projectfs-export")
parser.add_argument("--gateway-host", default="host.docker.internal")
parser.add_argument("--add-host-gateway", action="store_true")
args = parser.parse_args()
client_image = args.client_image or args.image
prefix = "agenticos-rust-nfs-" + uuid.uuid4().hex[:8]
root = Path(tempfile.mkdtemp(prefix=prefix + "-", dir=tempfile.gettempdir()))
data = root / "data"
data.mkdir()
data = data.resolve()
lease = str(uuid.uuid4())
(data / lease).mkdir()
(data / lease / "initial.txt").write_text("initial-live")
verifiers = {}
rpc_lock = threading.Lock()
calls = []


def local_path(value):
    # The HTTP fixture is reachable from Docker. Keep all request paths inside
    # its fresh synthetic directory even when exercising a malformed client.
    if value == ".":
        return data
    base = os.path.realpath(data)
    candidate = os.path.normpath(os.path.join(base, value))
    if not candidate.startswith(base + os.sep):
        raise OSError(errno.EACCES, "outside fixture")
    parts = value.split("/")
    if parts[0] != lease or any(part in ("", ".", "..") for part in parts):
        raise OSError(errno.EACCES, "outside lease")
    if os.path.realpath(candidate) != candidate:
        raise OSError(errno.EACCES, "symlinks unsupported")
    return Path(candidate)


for invalid_path in ("../escape", "/etc/passwd", lease + "/../escape", lease + "/../../escape"):
    try:
        local_path(invalid_path)
    except OSError as error:
        assert error.errno == errno.EACCES
    else:
        raise AssertionError("fixture admitted an escaping path")
link = data / lease / "escape-link"
link.symlink_to(root.parent)
try:
    local_path(lease + "/escape-link/outside")
except OSError as error:
    assert error.errno == errno.EACCES
else:
    raise AssertionError("fixture followed an escaping symlink")
finally:
    link.unlink()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def create_file(self, target, path, req):
        verifier = req.get("verifier")
        if verifier is not None and os.environ.get("REJECT_EXCLUSIVE_VERIFIER") == "1":
            raise OSError(errno.EOPNOTSUPP, "verifier unsupported")
        if target.exists() and req["exclusive"]:
            if verifier is None or verifiers.get(path) != verifier:
                raise FileExistsError(errno.EEXIST, "exists")
        elif not target.exists():
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, req["mode"])
            os.fsync(fd)
            os.close(fd)
            if verifier is not None:
                verifiers[path] = verifier

    def update_attributes(self, target, req):
        if "size" in req:
            with target.open("r+b") as f:
                f.truncate(req["size"])
                f.flush()
                os.fsync(f.fileno())
        if "mode" in req:
            target.chmod(req["mode"])
        if "modified_at" in req:
            import datetime

            t = datetime.datetime.fromisoformat(req["modified_at"]).timestamp()
            os.utime(target, (t, t))

    def execute(self, req):
        with rpc_lock:
            calls.append({k: v for k, v in req.items() if k != "data"})
            path = req["path"]
            target = local_path(path)

            def entry(p):
                info = p.stat()
                relative = p.relative_to(data).as_posix()
                return {
                    "path": relative,
                    "is_dir": p.is_dir(),
                    "size": info.st_size,
                    "mode": stat.S_IMODE(info.st_mode),
                    "modified_at": None,
                }

            op = req["op"]
            result = {"ok": True}
            if op == "stat":
                result["entry"] = entry(target)
            elif op == "list":
                result["entries"] = [entry(p) for p in target.iterdir()]
            elif op == "read":
                with target.open("rb") as f:
                    f.seek(req["offset"])
                    value = f.read(req["count"])
                result.update(
                    data=base64.b64encode(value).decode(),
                    eof=req["offset"] + len(value) >= target.stat().st_size,
                )
            elif op == "write":
                payload = base64.b64decode(req["data"], validate=True)
                with target.open("r+b") as f:
                    f.seek(req["offset"])
                    result["written"] = f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
            elif op == "create":
                self.create_file(target, path, req)
                result["entry"] = entry(target)
            elif op == "mkdir":
                target.mkdir(mode=req["mode"])
                result["entry"] = entry(target)
            elif op == "rename":
                destination = req["destination"]
                destination_path = local_path(destination)
                if destination_path == data:
                    raise OSError(errno.EACCES, "cannot rename lease root")
                target.rename(destination_path)
                if path in verifiers:
                    verifiers[destination] = verifiers.pop(path)
            elif op == "remove":
                target.rmdir() if req["directory"] else target.unlink()
            elif op == "setattr":
                self.update_attributes(target, req)
            else:
                raise OSError(errno.EOPNOTSUPP, "unsupported")
        return result

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer local-kernel-proof":
            self.send_error(403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size < 300000:
                raise OSError(errno.EINVAL, "invalid size")
            req = json.loads(self.rfile.read(size))
            result = self.execute(req)
        except OSError as e:
            result = {"ok": False, "errno": e.errno or 5, "message": "synthetic filesystem error"}
        except Exception:
            result = {"ok": False, "errno": 5, "message": "synthetic gateway failure"}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
network = prefix + "-private"
exporter = prefix + "-server"
client = prefix + "-client"
volume = prefix + "-volume"
result = {
    "reject_exclusive_verifier": os.environ.get("REJECT_EXCLUSIVE_VERIFIER") == "1",
    "prefix": prefix,
    "directory": str(root),
    "checks": {"path_containment": True},
    "commands": [],
}


def run(args, check=True, timeout=45):
    p = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=timeout)
    safe = ["[redacted local token]" if "PROJECTFS_TOKEN=" in a else a for a in args]
    result["commands"].append(
        {"args": safe, "code": p.returncode, "stdout": p.stdout[-3000:], "stderr": p.stderr[-3000:]}
    )
    if check and p.returncode:
        raise RuntimeError(f"{args[:2]} failed: {p.stderr[-1000:]} {p.stdout[-1000:]}")
    return p


try:
    run(["network", "create", "--label", "agenticos.proof=" + prefix, network])
    host_args = ["--add-host", "host.docker.internal:host-gateway"] if args.add_host_gateway else []
    result["image_id"] = run(["image", "inspect", args.image, "--format", "{{.Id}}"]).stdout.strip()
    run(
        [
            "run",
            "-d",
            "--name",
            exporter,
            "--label",
            "agenticos.proof=" + prefix,
            *host_args,
            "--entrypoint",
            args.exporter_entrypoint,
            "--health-cmd",
            args.exporter_entrypoint + " healthcheck",
            "--health-interval",
            "1s",
            "--health-timeout",
            "3s",
            "--health-start-period",
            "1s",
            "--health-retries",
            "5",
            "--network",
            network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "-e",
            f"PROJECTFS_RPC_URL=http://{args.gateway_host}:{server.server_port}/fs",
            "-e",
            "PROJECTFS_TOKEN=local-kernel-proof",
            args.image,
        ]
    )
    for _ in range(40):
        info = json.loads(run(["inspect", exporter]).stdout)[0]
        if info.get("State", {}).get("Health", {}).get("Status") == "healthy":
            break
        if info["State"]["Status"] == "exited":
            raise RuntimeError("exporter exited")
        time.sleep(0.3)
    assert info["State"].get("Health", {}).get("Status") == "healthy", "NFS readiness failed"
    result["checks"]["health"] = info["State"].get("Health", {})
    ip = info["NetworkSettings"]["Networks"][network]["IPAddress"]
    result["checks"]["published_ports"] = info["NetworkSettings"]["Ports"]
    run(
        [
            "volume",
            "create",
            "--label",
            "agenticos.proof=" + prefix,
            "--driver",
            "local",
            "--opt",
            "type=nfs",
            "--opt",
            "device=:/",
            "--opt",
            f"o=addr={ip},nfsvers=3,tcp,port=2049,mountport=2049,nolock,soft,timeo=10,retrans=1,lookupcache=none,noac,actimeo=0,sync",
            volume,
        ]
    )

    def start_client():
        run(
            [
                "run",
                "-d",
                "--name",
                client,
                "--label",
                "agenticos.proof=" + prefix,
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--user",
                "1000:1000",
                "--entrypoint",
                "python",
                "--mount",
                f"type=volume,source={volume},target=/project,volume-nocopy",
                client_image,
                "-c",
                "import time; time.sleep(300)",
            ]
        )

    start_client()
    script = """import os,tempfile,json
from pathlib import Path
p=Path('/project/LEASE')
assert (p/'initial.txt').read_text()=='initial-live'
with tempfile.NamedTemporaryFile(dir=p,delete=False) as f:
 f.write(b'binary\\x00bytes');f.flush();os.fsync(f.fileno());tmp=Path(f.name)
assert tmp.read_bytes()==b'binary\\x00bytes'
try: os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
except FileExistsError: pass
else: raise AssertionError('O_EXCL overwrote file')
new=p/'renamed.bin';tmp.rename(new);assert new.read_bytes()==b'binary\\x00bytes'
with new.open('r+b') as f: f.truncate(6); f.flush();os.fsync(f.fileno())
assert new.read_bytes()==b'binary'
(p/'subdir').mkdir();(p/'subdir').rmdir()
print(json.dumps({'tempfile':True,'exclusive':True,'binary':True,'rename':True,'truncate':True,'mkdir':True}))
""".replace("LEASE", lease)
    proof = run(["exec", client, "python", "-c", script])
    result["checks"]["normal_operations"] = json.loads(proof.stdout)
    result["checks"]["write_durable_at_gateway"] = (
        data / lease / "renamed.bin"
    ).read_bytes() == b"binary"
    (data / lease / "external.txt").write_text("external-change")
    result["checks"]["external_live"] = run(
        [
            "exec",
            client,
            "python",
            "-c",
            f"from pathlib import Path; p=Path('/project/{lease}/external.txt');"
            "assert p.read_text()=='external-change';print('passed')",
        ]
    ).stdout.strip()
    run(["restart", exporter])
    stale = run(
        [
            "exec",
            client,
            "python",
            "-c",
            f"from pathlib import Path;Path('/project/{lease}/initial.txt').read_text()",
        ],
        check=False,
    )
    result["checks"]["restart_requires_remount"] = stale.returncode != 0 and (
        "Stale" in stale.stderr or "stale" in stale.stderr
    )
    if not result["checks"]["restart_requires_remount"]:
        raise RuntimeError("helper restart did not produce expected stale handle")
    run(["rm", "-f", client])
    start_client()
    result["checks"]["explicit_remount_preserves_data"] = run(
        [
            "exec",
            client,
            "python",
            "-c",
            f"from pathlib import Path; p=Path('/project/{lease}/renamed.bin');"
            "assert p.read_bytes()==b'binary';print('passed')",
        ]
    ).stdout.strip()
    result["status"] = "passed"
except Exception as error:
    result["status"] = "failed"
    result["error"] = str(error)
finally:
    result["exporter_logs"] = run(["logs", exporter], check=False).stderr[-2000:]
    result["rpc_calls"] = calls
    for item in [client, exporter]:
        run(["rm", "-f", item], check=False)
    run(["volume", "rm", volume], check=False)
    run(["network", "rm", network], check=False)
    for kind, command in [
        ("containers", ["ps", "-a"]),
        ("networks", ["network", "ls"]),
        ("volumes", ["volume", "ls"]),
    ]:
        remaining = run(
            [
                *command,
                "--filter",
                "label=agenticos.proof=" + prefix,
                "--format",
                "{{.Name}}" if kind != "containers" else "{{.Names}}",
            ],
            check=False,
        )
        result["remaining_" + kind] = remaining.stdout.splitlines()
        if remaining.returncode or remaining.stdout.strip():
            result["status"] = "failed"
            result["error"] = "Docker resource cleanup failed"
    server.shutdown()
    server.server_close()
    (root / "result.json").write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                "status": result["status"],
                "checks": result["checks"],
                "error": result.get("error"),
                "result": str(root / "result.json"),
            },
            indent=2,
        ),
        flush=True,
    )

raise SystemExit(0 if result["status"] == "passed" else 1)
