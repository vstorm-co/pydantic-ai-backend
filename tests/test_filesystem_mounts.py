"""Trusted mount admission, immutable credentials and owned lifecycle boundaries."""

from __future__ import annotations

import json
import stat
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from docker import DockerClient
from docker.errors import NotFound
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from pydantic_ai.workspaces import WorkspaceUnavailableError

from pydantic_ai_backends.remote import _filesystem as fs
from pydantic_ai_backends.remote.server import SandboxdConfig, create_app, sweep_workspaces
from pydantic_ai_backends.remote.wire import CreateSessionRequest, FilesystemBinding
from pydantic_ai_backends.workspaces import SandboxdWorkspace, SandboxdWorkspaceBackend

BINDING = UUID("12345678-1234-4234-8234-123456789012")
TOKEN = "filesystem-sensitive-credential"


@pytest.fixture
def broker(tmp_path, monkeypatch):
    client = MagicMock(spec=DockerClient)
    stores = {"containers": {}, "networks": {}, "volumes": {}}
    for kind, store in stores.items():

        def lookup(name, store=store):
            if name not in store:
                raise NotFound("missing")
            return store[name]

        getattr(client, kind).get.side_effect = lookup
    mount = fs.FilesystemMountBroker(
        gateway_url="https://gateway/project-filesystem",
        image="export@sha256:abc",
        workspace_root=str(tmp_path),
        client=lambda: client,
    )
    monkeypatch.setattr(mount, "_verify", lambda record: None)

    def run(image, **kwargs):
        helper = MagicMock()
        helper.labels = kwargs["labels"]
        helper.attrs = {
            "Id": "export-id",
            "RestartCount": 0,
            "State": {
                "Status": "running",
                "StartedAt": "2026-11-26T12:00:00Z",
                "Health": {"Status": "healthy"},
            },
            "NetworkSettings": {"Networks": {kwargs["network"]: {"IPAddress": "172.30.0.2"}}},
        }
        helper.remove.side_effect = lambda: stores["containers"].pop(kwargs["name"])
        stores["containers"][kwargs["name"]] = helper
        return helper

    client.containers.run.side_effect = run

    def network(name, **kwargs):
        value = MagicMock()
        value.attrs = {"Labels": kwargs["labels"]}
        value.remove.side_effect = lambda: stores["networks"].pop(name)
        stores["networks"][name] = value
        return value

    client.networks.create.side_effect = network

    def volume(name, **kwargs):
        value = MagicMock()
        value.attrs = {"Labels": kwargs["labels"], "Options": kwargs["driver_opts"]}
        value.remove.side_effect = lambda: stores["volumes"].pop(name)
        stores["volumes"][name] = value
        return value

    client.volumes.create.side_effect = volume
    return mount, client, stores


def binding(token=TOKEN, id=BINDING):
    return FilesystemBinding(id=id, token=SecretStr(token))


def prepare(mount):
    mount.prepare("session-one", "org-one", binding(), existing=False, uid=1000)


def test_wire_refuses_arbitrary_mount_configuration_and_hides_tokens():
    value = binding()
    assert TOKEN not in repr(value)
    assert TOKEN not in value.model_dump_json()
    with pytest.raises(ValidationError):
        FilesystemBinding(id=BINDING, token=TOKEN, host_path="/etc")
    with pytest.raises(ValidationError):
        FilesystemBinding(id=BINDING, token="")
    assert TOKEN not in repr(CreateSessionRequest(filesystem_binding=value))


@pytest.mark.asyncio
async def test_sdk_explicitly_serializes_binding_only_at_transport_and_probes_policy():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/policy":
            return httpx.Response(
                200, json={"filesystem_mounts": True, "filesystem_mount_path": "/project"}
            )
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        capability = SandboxdWorkspace(
            service_url="https://sandbox",
            token="service",
            tenant="org-one",
            session_name="session-one",
            filesystem_binding=binding(),
            client=client,
        )
        await capability.backend()._create()
        assert requests[0].url.path == "/policy"
        body = json.loads(requests[1].content)
        assert body["filesystem_binding"] == {"id": str(BINDING), "token": TOKEN}
        assert body["session_id"] == "session-one"
    with pytest.raises(ValueError, match="named session"):
        SandboxdWorkspaceBackend("https://sandbox", token="service", filesystem_binding=binding())


@pytest.mark.asyncio
async def test_sdk_does_not_silently_ignore_binding_against_old_service():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        backend = SandboxdWorkspaceBackend(
            "https://sandbox",
            token="service",
            session_name="session-one",
            filesystem_binding=binding(),
            client=client,
        )
        with pytest.raises(WorkspaceUnavailableError, match="does not support"):
            await backend._create()
    assert len(requests) == 1
    assert requests[0].method == "GET"


def test_prepare_mounts_only_owned_named_volume_without_agent_privileges(broker):
    mount, client, stores = broker
    prepare(mount)
    name, net, volume = mount._names("session-one")
    assert mount.volume_for("session-one") == volume
    args = client.containers.run.call_args.kwargs
    assert args["network"] == net
    assert "ports" not in args and "privileged" not in args and "devices" not in args
    assert args["cap_drop"] == ["ALL"]
    assert args["security_opt"] == ["no-new-privileges:true"]
    assert args["environment"]["PROJECTFS_RPC_URL"].endswith(f"/bindings/{BINDING}/fs")
    assert args["environment"]["PROJECTFS_TOKEN"] == TOKEN
    assert "volumes" not in args
    assert client.volumes.create.call_args.kwargs["driver"] == "local"
    assert "noac" in stores["volumes"][volume].attrs["Options"]["o"]
    assert stat.S_IMODE(mount.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(mount._path("session-one").stat().st_mode) == 0o600
    mount.check("session-one")
    prepare(mount)
    assert client.containers.run.call_count == 1


@pytest.mark.parametrize("change", ["token", "binding", "tenant", "gateway", "image", "uid"])
def test_attach_refuses_changed_identity_without_replacing_resources(broker, change):
    mount, client, _ = broker
    prepare(mount)
    selected = (
        binding(token="different")
        if change == "token"
        else binding(id=uuid4())
        if change == "binding"
        else binding()
    )
    if change == "gateway":
        mount.gateway_url = "https://different"
    if change == "image":
        mount.image = "different"
    with pytest.raises(fs.FilesystemMountError, match="immutable"):
        mount.prepare(
            "session-one",
            "other-org" if change == "tenant" else "org-one",
            selected,
            existing=True,
            uid=1001 if change == "uid" else 1000,
        )
    assert client.containers.run.call_count == 1


def test_unbound_or_legacy_sessions_are_not_implicitly_migrated(broker):
    mount, client, _ = broker
    mount.prepare("unbound", None, None, existing=True, uid=1000)
    mount.check("unbound")
    with pytest.raises(fs.FilesystemMountError, match="named tenant"):
        mount.prepare("session-one", None, binding(), existing=False, uid=1000)
    with pytest.raises(fs.FilesystemMountError, match="no filesystem mount"):
        mount.prepare("session-one", "org-one", binding(), existing=True, uid=1000)
    prepare(mount)
    with pytest.raises(fs.FilesystemMountError, match="requires its existing"):
        mount.prepare("session-one", "org-one", None, existing=True, uid=1000)


@pytest.mark.parametrize(
    "failure", ["missing", "stopped", "restart", "id", "health", "nohealth", "address", "network"]
)
def test_changed_export_epoch_stops_agent_and_refuses_execution(broker, failure):
    mount, client, stores = broker
    prepare(mount)
    name = mount._names("session-one")[0]
    helper = stores["containers"][name]
    agent = MagicMock()
    stores["containers"]["sandboxd-session-one"] = agent
    if failure == "missing":
        del stores["containers"][name]
    elif failure == "stopped":
        helper.attrs["State"]["Status"] = "exited"
    elif failure == "restart":
        helper.attrs["RestartCount"] = 1
    elif failure == "id":
        helper.attrs["Id"] = "replacement"
    elif failure == "health":
        helper.attrs["State"]["Health"]["Status"] = "unhealthy"
    elif failure == "address":
        helper.attrs["NetworkSettings"]["Networks"][mount._names("session-one")[1]]["IPAddress"] = (
            "172.30.0.3"
        )
    elif failure == "network":
        helper.attrs["NetworkSettings"]["Networks"]["untrusted"] = {"IPAddress": "10.0.0.1"}
    else:
        del helper.attrs["State"]["Health"]
    with pytest.raises(fs.FilesystemMountError, match="explicitly"):
        mount.check("session-one")
    agent.stop.assert_called_once_with(timeout=5)
    helper.start.assert_not_called()


def test_idle_retained_container_keeps_same_export_and_purge_reclaims_resources(broker):
    mount, client, stores = broker
    prepare(mount)
    name = mount._names("session-one")[0]
    helper = stores["containers"][name]
    stores["containers"]["sandboxd-session-one"] = MagicMock()
    mount.suspend("session-one")
    helper.stop.assert_not_called()
    mount.prepare("session-one", "org-one", binding(), existing=True, uid=1000)
    assert client.containers.run.call_count == 1
    del stores["containers"]["sandboxd-session-one"]
    mount.suspend("session-one", purge=True)
    assert not any(stores.values())
    assert not mount._path("session-one").exists()


def test_restarted_broker_reattaches_only_same_helper_and_metadata(broker, monkeypatch):
    mount, client, _ = broker
    prepare(mount)
    restored = fs.FilesystemMountBroker(
        gateway_url=mount.gateway_url,
        image=mount.image,
        workspace_root=str(mount.root.parent),
        client=lambda: client,
    )
    monkeypatch.setattr(restored, "_verify", lambda record: None)
    restored.prepare("session-one", "org-one", binding(), existing=True, uid=1000)
    assert client.containers.run.call_count == 1
    assert restored.volume_for("session-one") == mount.volume_for("session-one")


def test_gateway_proof_checks_all_identity_fields_and_does_not_leak_error_body(broker, monkeypatch):
    mount, _, _ = broker
    monkeypatch.setattr(mount, "_verify", fs.FilesystemMountBroker._verify.__get__(mount))
    record = fs._Record(
        session_name="session-one",
        tenant="org-one",
        uid=1000,
        binding=binding(),
        gateway_url=mount.gateway_url,
        image=mount.image,
    )
    original = httpx.Client

    def respond(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.path.endswith(f"/bindings/{BINDING}")
        return httpx.Response(
            200,
            json={
                "binding_id": str(BINDING),
                "session_name": "wrong",
                "tenant": "org-one",
                "mount_path": "/project",
            },
        )

    monkeypatch.setattr(
        fs.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond))
    )
    with pytest.raises(fs.FilesystemMountError, match="does not match") as error:
        mount._verify(record)
    assert TOKEN not in str(error.value)


def test_disabled_service_rejects_binding_and_redacts_invalid_body(tmp_path):
    app = create_app(
        SandboxdConfig(token="service", prewarm=False), sandbox_builder=lambda *args: None
    )
    with TestClient(app) as client:
        response = client.post(
            "/sessions",
            headers={"X-Sandbox-Token": "service"},
            json={
                "session_id": "session-one",
                "filesystem_binding": {"id": str(BINDING), "token": TOKEN},
            },
        )
        assert response.status_code == 400
        invalid = client.post(
            "/sessions", headers={"X-Sandbox-Token": "service"}, json={"filesystem_binding": TOKEN}
        )
        assert invalid.status_code == 422
        assert TOKEN not in invalid.text
        standard = client.post(
            "/sessions", headers={"X-Sandbox-Token": "service"}, json={"session_id": "../bad"}
        )
        assert standard.status_code == 422
    reserved = tmp_path / fs.STATE_DIRECTORY
    reserved.mkdir()
    assert (
        sweep_workspaces(
            SandboxdConfig(token="service", workspace_root=str(tmp_path), workspace_ttl=0),
            set(),
            9999999999,
        )
        == []
    )
    assert reserved.exists()


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp",
        "http:///missing",
        "http://name@host",
        "http://:password@host",
        "http://host?query=token",
        "http://host/#fragment",
    ],
)
def test_operator_gateway_cannot_embed_credential_or_non_http_url(tmp_path, url):
    with pytest.raises(ValueError, match="credential-free"):
        fs.FilesystemMountBroker(
            gateway_url=url, image="image", workspace_root=str(tmp_path), client=MagicMock()
        )


def test_empty_operator_image_and_symlinked_metadata_are_refused(tmp_path):
    with pytest.raises(ValueError, match="image"):
        fs.FilesystemMountBroker(
            gateway_url="http://gateway",
            image=" ",
            workspace_root=str(tmp_path),
            client=MagicMock(),
        )
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / fs.STATE_DIRECTORY).symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        fs.FilesystemMountBroker(
            gateway_url="http://gateway",
            image="image",
            workspace_root=str(tmp_path),
            client=MagicMock(),
        )


@pytest.mark.parametrize("name", ["", ".", "..", "../other", "slash/name", "bad name"])
def test_internal_state_path_does_not_allow_traversal(broker, name):
    with pytest.raises(fs.FilesystemMountError, match="identity"):
        broker[0]._path(name)


def test_corrupted_and_symlinked_records_are_not_followed(broker, tmp_path):
    mount, _, _ = broker
    path = mount._path("session-one")
    path.write_text("not json")
    with pytest.raises(fs.FilesystemMountError, match="metadata"):
        mount.check("session-one")
    path.unlink()
    target = tmp_path / "do-not-read"
    target.write_text(TOKEN)
    path.symlink_to(target)
    with pytest.raises(fs.FilesystemMountError, match="metadata") as error:
        mount.check("session-one")
    assert TOKEN not in str(error.value)


@pytest.mark.parametrize(
    "reply",
    [
        "good",
        "malformed",
        "denied",
        "wrong-binding",
        "wrong-tenant",
        "wrong-mount",
        "missing-mount",
    ],
)
def test_gateway_transport_validation(broker, monkeypatch, reply):
    mount, _, _ = broker
    monkeypatch.setattr(mount, "_verify", fs.FilesystemMountBroker._verify.__get__(mount))
    record = fs._Record(
        session_name="session-one",
        tenant="org-one",
        uid=1000,
        binding=binding(),
        gateway_url=mount.gateway_url,
        image=mount.image,
    )
    original = httpx.Client
    proof = {
        "binding_id": str(BINDING),
        "session_name": "session-one",
        "tenant": "org-one",
        "mount_path": "/project",
    }
    if reply == "wrong-binding":
        proof["binding_id"] = str(uuid4())
    if reply == "wrong-tenant":
        proof["tenant"] = "other"
    if reply == "wrong-mount":
        proof["mount_path"] = "/etc"
    if reply == "missing-mount":
        del proof["mount_path"]

    def respond(request):
        if reply == "denied":
            return httpx.Response(403, text=TOKEN)
        if reply == "malformed":
            return httpx.Response(200, text=TOKEN)
        return httpx.Response(200, json=proof)

    monkeypatch.setattr(
        fs.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond))
    )
    if reply == "good":
        mount._verify(record)
    else:
        with pytest.raises(fs.FilesystemMountError) as error:
            mount._verify(record)
        assert TOKEN not in str(error.value)


@pytest.mark.parametrize(
    "failure",
    ["labels", "epoch", "address", "volume-labels", "volume-options", "missing-retained-export"],
)
def test_admission_refuses_resource_replacement(broker, failure):
    mount, _, stores = broker
    prepare(mount)
    name, network, volume = mount._names("session-one")
    helper = stores["containers"][name]
    if failure == "labels":
        helper.labels = {}
    elif failure == "epoch":
        helper.attrs["State"]["StartedAt"] = "later"
    elif failure == "address":
        helper.attrs["NetworkSettings"]["Networks"][network]["IPAddress"] = "172.30.0.99"
    elif failure == "volume-labels":
        stores["volumes"][volume].attrs["Labels"] = {}
    elif failure == "volume-options":
        stores["volumes"][volume].attrs["Options"] = {}
    else:
        del stores["containers"][name]
        stores["containers"]["sandboxd-session-one"] = MagicMock()
    with pytest.raises(fs.FilesystemMountError, match="could not be prepared"):
        prepare(mount)
    helper.start.assert_not_called()


def test_colliding_control_network_is_never_adopted(broker):
    mount, _, stores = broker
    network = MagicMock()
    network.attrs = {"Labels": {}}
    stores["networks"][mount._names("session-one")[1]] = network
    with pytest.raises(fs.FilesystemMountError, match="could not be prepared"):
        prepare(mount)


@pytest.mark.parametrize("failure", ["stopped", "no-health", "never-ready", "becomes-ready"])
def test_readiness_uses_docker_health_without_exposing_export_network(broker, monkeypatch, failure):
    mount, client, stores = broker
    prepare(mount)
    helper = stores["containers"][mount._names("session-one")[0]]
    if failure == "stopped":
        helper.attrs["State"]["Status"] = "exited"
    elif failure == "no-health":
        del helper.attrs["State"]["Health"]
    else:
        helper.attrs["State"]["Health"]["Status"] = "starting"

    def sleep(seconds):
        if failure == "becomes-ready":
            helper.attrs["State"]["Health"]["Status"] = "healthy"

    monkeypatch.setattr(fs.time, "sleep", sleep)
    if failure == "becomes-ready":
        assert mount._wait(helper, mount._names("session-one")[1]) == "172.30.0.2"
    else:
        with pytest.raises(fs.FilesystemMountError):
            mount._wait(helper, mount._names("session-one")[1])


def test_cleanup_empty_or_partially_created_resources_and_sweep(broker):
    mount, _, stores = broker
    mount.suspend("not-created", purge=True)
    prepare(mount)
    del stores["containers"][mount._names("session-one")[0]]
    mount.sweep({"session-one"})
    assert stores["volumes"]
    (mount.root.parent / "session-one").mkdir()
    mount.sweep(set())
    assert not any(stores.values())
    assert mount._path("session-one").exists()
    (mount.root.parent / "session-one").rmdir()
    mount.sweep(set())
    assert not mount._path("session-one").exists()


@pytest.mark.parametrize("resource", ["export", "volume", "network"])
def test_cleanup_cannot_remove_an_unowned_resource(broker, resource):
    mount, _, stores = broker
    prepare(mount)
    name, network, volume = mount._names("session-one")
    if resource == "export":
        stores["containers"][name].labels = {}
    elif resource == "volume":
        stores["volumes"][volume].attrs["Labels"] = {}
    else:
        stores["networks"][network].attrs["Labels"] = {}
    with pytest.raises(fs.FilesystemMountError, match="unowned"):
        mount.suspend("session-one", purge=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"filesystem_gateway_url": "http://gateway"},
        {"filesystem_export_image": "image"},
        {"filesystem_gateway_url": "http://gateway", "filesystem_export_image": "image"},
        {
            "filesystem_gateway_url": "http://gateway",
            "filesystem_export_image": "image",
            "persist_containers": True,
        },
    ],
)
def test_mount_config_is_opt_in_and_requires_durable_session_lifecycle(kwargs):
    with pytest.raises(ValueError):
        SandboxdConfig(token="service", **kwargs)


def test_enabled_service_admission_execution_and_purge_use_broker(tmp_path, monkeypatch):
    from pydantic_ai_backends.remote import server
    from tests.test_remote_sandbox import FakeSandbox

    mount = MagicMock(spec=fs.FilesystemMountBroker)
    monkeypatch.setattr(server, "FilesystemMountBroker", lambda **kwargs: mount)
    monkeypatch.setattr(server, "_default_builder", lambda *args, **kwargs: FakeSandbox)
    monkeypatch.setattr(server, "_default_prewarm", lambda config: None)
    monkeypatch.setattr(server, "_default_docker_client", lambda: None)
    config = SandboxdConfig(
        token="service",
        prewarm=False,
        workspace_root=str(tmp_path),
        persist_containers=True,
        filesystem_gateway_url="https://gateway",
        filesystem_export_image="trusted-export",
    )
    app = create_app(config)
    headers = {"X-Sandbox-Token": "service"}
    payload = {
        "session_id": "session-one",
        "tenant": "org-one",
        "filesystem_binding": {"id": str(BINDING), "token": TOKEN},
    }
    with TestClient(app) as client:
        assert client.get("/policy", headers=headers).json()["filesystem_mounts"] is True
        missing_name = client.post(
            "/sessions", headers=headers, json={"filesystem_binding": payload["filesystem_binding"]}
        )
        assert missing_name.status_code == 400
        response = client.post("/sessions", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        assert TOKEN not in response.text
        session_headers = {"X-Sandbox-Token": response.json()["token"]}
        attached = client.post("/sessions", headers=headers, json={**payload, "reuse": True})
        assert attached.status_code == 200
        assert mount.prepare.call_count == 2
        mount.check.side_effect = fs.FilesystemMountError("filesystem explicitly unavailable")
        failed = client.post(
            "/sessions/session-one/run",
            headers=session_headers,
            json={"argv": ["true"], "run_id": "0" * 32},
        )
        assert failed.status_code == 503, failed.text
        mount.check.side_effect = None
        mount.prepare.side_effect = fs.FilesystemMountError("binding refused")
        refused = client.post("/sessions", headers=headers, json={**payload, "reuse": True})
        assert refused.status_code == 412
        mount.prepare.side_effect = None
        closed = client.delete("/sessions/session-one?purge=true", headers=session_headers)
        assert closed.status_code == 204, closed.text
        mount.suspend.assert_called_with("session-one", purge=True)
        unbound = client.post("/sessions", headers=headers, json={"session_id": "plain"})
        assert unbound.status_code == 200
        assert mount.prepare.call_args.args[2] is None
        app.state.service._pending["opening"] = MagicMock()
        app.state.service._sweep_once()
        assert "opening" in mount.sweep.call_args.args[0]
        app.state.service._pending.clear()
    assert mount.sweep.called


@pytest.mark.parametrize("mode", ["host", "container:another", "projectfs-private"])
def test_enabled_service_refuses_control_plane_network_sharing(tmp_path, mode):
    config = SandboxdConfig(
        token="service",
        persist_containers=True,
        workspace_root=str(tmp_path),
        filesystem_gateway_url="https://gateway",
        filesystem_export_image="trusted-export",
        network_mode=mode,
    )
    with pytest.raises(ValueError, match="network sharing"):
        create_app(config)


def test_enabled_service_cannot_use_uncontrolled_builder(tmp_path):
    config = SandboxdConfig(
        token="service",
        persist_containers=True,
        workspace_root=str(tmp_path),
        filesystem_gateway_url="https://gateway",
        filesystem_export_image="trusted-export",
    )
    with pytest.raises(ValueError, match="default Docker builder"):
        create_app(config, sandbox_builder=lambda *args: None)


def test_default_builder_adds_project_mount_without_replacing_own_workspace(tmp_path):
    from pydantic_ai_backends.remote.server import SandboxRuntime, _default_builder

    config = SandboxdConfig(token="service", workspace_root=str(tmp_path), persist_containers=True)
    mounted = _default_builder(config, filesystem_volume=lambda name: "trusted-volume")(
        "session-one", SandboxRuntime(image="python:3.12-slim")
    )
    assert mounted._volumes == {
        str(tmp_path / "session-one" / "workspace"): "/workspace",
        "trusted-volume": "/project",
    }
    plain = _default_builder(config, filesystem_volume=lambda name: None)(
        "session-two", SandboxRuntime(image="python:3.12-slim")
    )
    assert plain._volumes == {str(tmp_path / "session-two" / "workspace"): "/workspace"}


def test_sdk_requires_explicit_session_for_binding():
    with pytest.raises(ValueError, match="named session"):
        SandboxdWorkspaceBackend("https://sandbox", token="service", filesystem_binding=binding())


def test_gateway_revocation_stops_existing_agent_without_replacing_files(broker, monkeypatch):
    mount, _, stores = broker
    prepare(mount)
    agent = MagicMock()
    stores["containers"]["sandboxd-session-one"] = agent

    def revoked(record):
        raise fs.FilesystemMountError("revoked binding")

    monkeypatch.setattr(mount, "_verify", revoked)
    with pytest.raises(fs.FilesystemMountError, match="explicitly"):
        mount.check("session-one")
    agent.stop.assert_called_once_with(timeout=5)
    assert stores["volumes"]


def test_metadata_storage_errors_do_not_expose_physical_paths(broker, monkeypatch):
    mount, _, _ = broker

    def failed(record):
        raise OSError("private physical path and credential")

    monkeypatch.setattr(mount, "_write", failed)
    with pytest.raises(fs.FilesystemMountError, match="could not be prepared") as error:
        prepare(mount)
    assert "private physical" not in str(error.value)


def test_environment_enables_both_operator_owned_mount_settings(tmp_path):
    from pydantic_ai_backends.remote.env import config_from_env

    config = config_from_env(
        {
            "SANDBOXD_TOKEN": "service",
            "SANDBOXD_WORKSPACE_ROOT": str(tmp_path),
            "SANDBOXD_PERSIST_CONTAINERS": "true",
            "SANDBOXD_FILESYSTEM_GATEWAY_URL": "https://gateway",
            "SANDBOXD_FILESYSTEM_EXPORT_IMAGE": "sandboxd@sha256:abc",
        }
    )
    assert config.filesystem_gateway_url == "https://gateway"
    assert config.filesystem_export_image == "sandboxd@sha256:abc"


def test_binding_metadata_is_durable_before_mount_and_directory_sync_failure_refuses(
    broker, monkeypatch
):
    mount, _, _ = broker
    original = fs.os.fsync
    synced = []

    def sync(fd):
        is_directory = stat.S_ISDIR(fs.os.fstat(fd).st_mode)
        synced.append(is_directory)
        if is_directory:
            assert mount._read("session-one").binding.id == BINDING
        original(fd)

    monkeypatch.setattr(fs.os, "fsync", sync)
    prepare(mount)
    assert synced == [False, True, False, True]

    def fail_directory(fd):
        if stat.S_ISDIR(fs.os.fstat(fd).st_mode):
            raise OSError("durability unavailable")
        original(fd)

    monkeypatch.setattr(fs.os, "fsync", fail_directory)
    with pytest.raises(fs.FilesystemMountError, match="could not be prepared"):
        prepare(mount)


@pytest.mark.parametrize(
    "runtime_uid,operator_uid,ready,expected",
    [
        (1234, None, False, 1234),
        (1234, 2345, False, 2345),
        (None, None, False, 0),
        (None, 3456, True, 3456),
        (None, None, True, None),
    ],
)
def test_mount_ownership_comes_from_effective_operator_runtime(
    tmp_path, monkeypatch, runtime_uid, operator_uid, ready, expected
):
    from pydantic_ai_backends.remote import server
    from pydantic_ai_backends.types import RuntimeConfig
    from tests.test_remote_sandbox import FakeSandbox

    mount = MagicMock(spec=fs.FilesystemMountBroker)
    monkeypatch.setattr(server, "FilesystemMountBroker", lambda **kwargs: mount)
    monkeypatch.setattr(server, "_default_builder", lambda *args, **kwargs: FakeSandbox)
    monkeypatch.setattr(server, "_default_prewarm", lambda config: None)
    monkeypatch.setattr(server, "_default_docker_client", lambda: None)
    runtime = (
        server.SandboxRuntime(image="python:3.12-slim")
        if ready
        else server.SandboxRuntime(
            runtime=RuntimeConfig(
                name="controlled", base_image="python:3.12-slim", run_as_uid=runtime_uid
            )
        )
    )
    app = create_app(
        SandboxdConfig(
            token="service",
            runtimes={"controlled": runtime},
            sandbox_uid=operator_uid,
            persist_containers=True,
            workspace_root=str(tmp_path),
            filesystem_gateway_url="https://gateway",
            filesystem_export_image="trusted-export",
            prewarm=False,
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/sessions",
            headers={"X-Sandbox-Token": "service"},
            json={
                "session_id": "session-one",
                "tenant": "org-one",
                "filesystem_binding": {"id": str(BINDING), "token": TOKEN},
            },
        )
        if expected is None:
            assert response.status_code == 412
            assert "numeric runtime identity" in response.text
            mount.prepare.assert_not_called()
        else:
            assert response.status_code == 200, response.text
            assert mount.prepare.call_args.kwargs["uid"] == expected


def test_exporter_presents_the_admitted_runtime_identity(broker):
    mount, client, _ = broker
    mount.prepare("session-one", "org-one", binding(), existing=False, uid=2345)
    environment = client.containers.run.call_args.kwargs["environment"]
    assert environment["PROJECTFS_UID"] == "2345"
    assert environment["PROJECTFS_GID"] == "2345"
    assert mount._read("session-one").uid == 2345


@pytest.mark.parametrize("work_dir", ["/project", "/project/nested"])
def test_live_mount_cannot_replace_the_agents_own_workspace(tmp_path, work_dir):
    with pytest.raises(ValueError, match="outside the additional"):
        SandboxdConfig(
            token="service",
            work_dir=work_dir,
            persist_containers=True,
            workspace_root=str(tmp_path),
            filesystem_gateway_url="https://gateway",
            filesystem_export_image="trusted-export",
        )
