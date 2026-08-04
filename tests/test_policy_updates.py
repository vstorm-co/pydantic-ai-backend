"""Changing the ceilings without a restart, and refusing to change anything else.

An operator running `sandboxd` for several teams had about thirty knobs reachable
only through the environment — and a restart drops every resident sandbox, so
raising one memory ceiling ended every conversation on the host.

Two ways in now, and the tests that matter most are the refusals.
`CreateSessionRequest` carries no container settings because a process holding the
Docker socket can start a privileged container that mounts the host; that reasoning
does not stop applying because the caller holds the service token. In a
multi-tenant deployment the token is held by an application, per tenant, and an
organization's administrator is not the person who runs the host.

So: ceilings and lifetimes are writable. Images, mounts, `network_mode`,
`oci_runtime`, `sandbox_uid` and `work_dir` are not, and an attempt to send one is
a refusal that names the field rather than a silently ignored key.
"""

from __future__ import annotations

import json

import pytest

from pydantic_ai_backends.remote import wire
from pydantic_ai_backends.remote.server import (
    POLICY_SCALARS,
    PolicyOverridesWatcher,
    PolicyUpdateError,
    SandboxdConfig,
    SandboxRuntime,
    apply_policy_update,
    load_policy_overrides,
)


def config(**overrides: object) -> SandboxdConfig:
    return SandboxdConfig(
        token="service-token",
        runtimes={
            "coding": SandboxRuntime(image="python:3.12", network_mode="bridge"),
            "locked": SandboxRuntime(image="python:3.12-slim", mem_limit="256m"),
        },
        default_runtime="coding",
        **overrides,  # type: ignore[arg-type]
    )


class TestWhatMayChange:
    def test_a_service_wide_ceiling_is_written_and_reported(self) -> None:
        live = config(mem_limit="512m")

        changed = apply_policy_update(live, wire.PolicyUpdate(mem_limit="1g"))

        assert live.mem_limit == "1g"
        assert changed == ["mem_limit"]

    def test_a_lifetime_is_a_ceiling_too(self) -> None:
        live = config()

        apply_policy_update(live, wire.PolicyUpdate(workspace_ttl=3600, idle_timeout=60))

        assert live.workspace_ttl == 3600
        assert live.idle_timeout == 60

    def test_one_runtimes_ceilings_can_be_raised(self) -> None:
        live = config()

        changed = apply_policy_update(
            live, wire.PolicyUpdate(runtimes={"locked": wire.RuntimeLimitsUpdate(mem_limit="2g")})
        )

        assert live.runtimes["locked"].mem_limit == "2g"  # type: ignore[union-attr]
        assert changed == ["runtimes.locked.mem_limit"]

    def test_a_runtime_stored_as_a_bare_image_string_still_takes_ceilings(self) -> None:
        """The allowlist may hold a plain string, which `_as_runtime` turns into a
        fresh object — so without writing it back the change would land on a
        temporary nobody else ever sees."""
        live = config()
        live.runtimes["bare"] = "python:3.12"  # type: ignore[assignment]

        apply_policy_update(
            live, wire.PolicyUpdate(runtimes={"bare": wire.RuntimeLimitsUpdate(cpus=2.0)})
        )

        assert live.runtimes["bare"].cpus == 2.0  # type: ignore[union-attr]

    def test_the_default_runtime_can_be_switched_between_allowed_ones(self) -> None:
        live = config()

        apply_policy_update(live, wire.PolicyUpdate(default_runtime="locked"))

        assert live.default_runtime == "locked"

    def test_absent_means_leave_it_alone(self) -> None:
        """An operator raising one ceiling must not have to restate the other
        twenty — and `None` is a meaningful value for most of these, so a model
        that could not tell absent from null would wipe one every time."""
        live = config(mem_limit="512m", cpus=1.5)

        apply_policy_update(live, wire.PolicyUpdate(pids_limit=256))

        assert live.mem_limit == "512m"
        assert live.cpus == 1.5

    def test_null_is_distinguishable_from_absent(self) -> None:
        """Explicitly clearing a ceiling is a thing an operator may want: `None` on
        `memswap_limit` pins swap to memory."""
        live = config(memswap_limit="1g")

        apply_policy_update(live, wire.PolicyUpdate.model_validate({"memswap_limit": None}))

        assert live.memswap_limit is None

    def test_asking_for_what_is_already_true_changes_nothing(self) -> None:
        """Reported as no change rather than as a change, so a log line means
        something actually moved."""
        live = config(mem_limit="512m")

        assert apply_policy_update(live, wire.PolicyUpdate(mem_limit="512m")) == []


class TestWhatMayNot:
    @pytest.mark.parametrize(
        "field",
        [
            "network_mode",
            "oci_runtime",
            "sandbox_uid",
            "work_dir",
            "workspace_root",
            "persist_containers",
            "prewarm",
            "runtime",
            "image",
            "volumes",
            "mounts",
            "devices",
            "privileged",
        ],
    )
    def test_nothing_that_decides_isolation_is_even_accepted(self, field: str) -> None:
        """`extra="forbid"`, so this is a 422 naming the field rather than a key
        quietly dropped — an operator who tries to widen the network gets told no,
        instead of believing they have."""
        with pytest.raises(ValueError, match=field):
            wire.PolicyUpdate.model_validate({field: "anything"})

    def test_a_runtime_cannot_be_given_an_image_through_its_ceilings(self) -> None:
        """The door this endpoint is not. What a runtime *is* stays the daemon's."""
        with pytest.raises(ValueError, match="image"):
            wire.RuntimeLimitsUpdate.model_validate({"image": "evil:latest"})

    def test_an_unknown_runtime_is_refused_rather_than_created(self) -> None:
        live = config()

        with pytest.raises(PolicyUpdateError, match="Unknown runtime 'invented'"):
            apply_policy_update(
                live,
                wire.PolicyUpdate(runtimes={"invented": wire.RuntimeLimitsUpdate(cpus=8.0)}),
            )

        assert "invented" not in live.runtimes

    def test_an_unknown_default_runtime_is_refused(self) -> None:
        live = config()

        with pytest.raises(PolicyUpdateError, match="Unknown runtime 'nope'"):
            apply_policy_update(live, wire.PolicyUpdate(default_runtime="nope"))

        assert live.default_runtime == "coding"

    @pytest.mark.parametrize(
        ("field", "value"),
        [("cpus", 0), ("cpu_shares", 0), ("pids_limit", 0), ("execute_timeout", 0)],
    )
    def test_a_ceiling_of_zero_where_zero_is_meaningless_is_refused(
        self, field: str, value: int
    ) -> None:
        """Zero CPUs is not a tighter ceiling, it is a sandbox that cannot run.
        Refused at the model so it never reaches the daemon."""
        with pytest.raises(ValueError):
            wire.PolicyUpdate.model_validate({field: value})

    def test_the_writable_list_and_the_model_agree(self) -> None:
        """Two places on purpose, so widening the wire shape is not the same act as
        widening what gets written — and this fails if somebody does one without
        the other.
        """
        modelled = set(wire.PolicyUpdate.model_fields) - {"default_runtime", "runtimes"}

        assert modelled == set(POLICY_SCALARS)


class TestTheEndpoint:
    def test_a_ceiling_is_changed_and_the_whole_policy_comes_back(self, tmp_path) -> None:
        """The whole policy rather than an acknowledgement, so a caller sees what is
        in force including the fields it did not touch."""
        from tests.test_remote_sandbox import SERVICE_TOKEN, Harness

        harness = Harness(workspace_root=str(tmp_path))
        with harness.client() as client:
            answer = client.put(
                "/policy",
                json={"mem_limit": "1g", "workspace_ttl": 7200},
                headers={wire.TOKEN_HEADER: SERVICE_TOKEN},
            )

            assert answer.status_code == 200
            assert answer.json()["mem_limit"] == "1g"
            assert answer.json()["workspace_ttl"] == 7200
            # Still there, which is the point of answering with all of it.
            assert answer.json()["work_dir"] != ""

    def test_it_needs_the_service_token(self, tmp_path) -> None:
        """The ceilings are the host's. Reading them is already authenticated;
        writing them cannot be less so."""
        from tests.test_remote_sandbox import Harness

        harness = Harness(workspace_root=str(tmp_path))
        with harness.client() as client:
            assert client.put("/policy", json={"mem_limit": "1g"}).status_code == 401

    def test_an_unknown_runtime_is_a_bad_request_not_a_crash(self, tmp_path) -> None:
        from tests.test_remote_sandbox import SERVICE_TOKEN, Harness

        harness = Harness(workspace_root=str(tmp_path))
        with harness.client() as client:
            answer = client.put(
                "/policy",
                json={"default_runtime": "nope"},
                headers={wire.TOKEN_HEADER: SERVICE_TOKEN},
            )

        assert answer.status_code == 400
        assert "Unknown runtime" in answer.json()["detail"]

    def test_a_forbidden_field_is_refused_by_name(self, tmp_path) -> None:
        from tests.test_remote_sandbox import SERVICE_TOKEN, Harness

        harness = Harness(workspace_root=str(tmp_path))
        with harness.client() as client:
            answer = client.put(
                "/policy",
                json={"network_mode": "host"},
                headers={wire.TOKEN_HEADER: SERVICE_TOKEN},
            )

        assert answer.status_code == 422
        assert "network_mode" in answer.text


class TestTheOverridesFile:
    def test_it_is_applied_when_it_is_there(self, tmp_path) -> None:
        live = config(mem_limit="512m")
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"mem_limit": "4g", "max_sessions": 5}))

        changed = load_policy_overrides(live, path)

        assert live.mem_limit == "4g"
        assert live.max_sessions == 5
        assert sorted(changed) == ["max_sessions", "mem_limit"]

    def test_an_absent_file_is_the_normal_state(self, tmp_path) -> None:
        """The setting exists so an operator can drop one in later, so its absence
        cannot be an error."""
        live = config()

        assert load_policy_overrides(live, tmp_path / "nothing.json") == []

    def test_malformed_json_is_logged_and_the_previous_value_kept(self, tmp_path) -> None:
        """A mistyped ceiling must not stop a running service or prevent one from
        starting — that would take every resident sandbox with it."""
        live = config(mem_limit="512m")
        path = tmp_path / "policy.json"
        path.write_text("{not json")

        assert load_policy_overrides(live, path) == []
        assert live.mem_limit == "512m"

    def test_a_forbidden_field_in_the_file_is_ignored_not_obeyed(self, tmp_path) -> None:
        """The same writable set as the endpoint, which is the whole reason both go
        through one function: a deployment that chose the file over the endpoint
        does not get a wider door for having chosen it."""
        live = config()
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"network_mode": "host"}))

        assert load_policy_overrides(live, path) == []
        assert live.network_mode != "host"

    def test_an_unknown_runtime_in_the_file_is_ignored(self, tmp_path) -> None:
        live = config()
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"runtimes": {"invented": {"cpus": 8.0}}}))

        assert load_policy_overrides(live, path) == []
        assert "invented" not in live.runtimes

    def test_a_file_that_cannot_be_read_is_logged_not_raised(self, tmp_path) -> None:
        live = config()
        directory = tmp_path / "policy.json"
        directory.mkdir()

        assert load_policy_overrides(live, directory) == []


class TestTheWatcher:
    def test_it_reads_the_file_once_and_then_only_on_a_change(self, tmp_path) -> None:
        live = config(mem_limit="512m")
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"mem_limit": "1g"}))
        watcher = PolicyOverridesWatcher(live, path)

        assert watcher.poll() == ["mem_limit"]
        assert watcher.poll() == []

    def test_it_notices_an_edit(self, tmp_path) -> None:
        live = config(mem_limit="512m")
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"mem_limit": "1g"}))
        watcher = PolicyOverridesWatcher(live, path)
        watcher.poll()

        # The mtime is what it watches, and a same-second rewrite can land on the
        # same value — so it is moved explicitly rather than by writing quickly.
        path.write_text(json.dumps({"mem_limit": "2g"}))
        import os

        stamp = path.stat().st_mtime + 10
        os.utime(path, (stamp, stamp))

        assert watcher.poll() == ["mem_limit"]
        assert live.mem_limit == "2g"

    def test_an_absent_file_polls_to_nothing(self, tmp_path) -> None:
        live = config()
        watcher = PolicyOverridesWatcher(live, tmp_path / "nothing.json")

        assert watcher.poll() == []

    def test_a_deleted_file_is_left_alone_rather_than_reverted(self, tmp_path) -> None:
        """Reverting would mean remembering the environment's values and putting
        them back, which is a second source of truth. The honest reading of a
        deleted overrides file is "stop overriding from here", which is a restart.
        """
        live = config(mem_limit="512m")
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"mem_limit": "1g"}))
        watcher = PolicyOverridesWatcher(live, path)
        watcher.poll()

        path.unlink()

        assert watcher.poll() == []
        assert live.mem_limit == "1g"

    def test_no_file_configured_builds_no_watcher(self) -> None:
        """So the sweep loop skips even the `stat` for every deployment not using
        it, which is all of them by default."""
        from pydantic_ai_backends.remote.server import _Service

        service = _Service(config(), lambda session_id, runtime: None)

        assert service._policy_watcher is None


class TestNothingToDo:
    """The "already true" paths, which are what makes a log line mean something."""

    def test_setting_the_default_runtime_to_what_it_is_reports_no_change(self) -> None:
        live = config()

        assert apply_policy_update(live, wire.PolicyUpdate(default_runtime="coding")) == []

    def test_a_runtimes_update_that_matches_reports_no_change(self) -> None:
        live = config()

        changed = apply_policy_update(
            live,
            wire.PolicyUpdate(runtimes={"locked": wire.RuntimeLimitsUpdate(mem_limit="256m")}),
        )

        assert changed == []

    def test_an_overrides_file_that_matches_reports_no_change(self, tmp_path) -> None:
        live = config(mem_limit="512m")
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"mem_limit": "512m"}))

        assert load_policy_overrides(live, path) == []

    def test_a_put_that_changes_nothing_still_answers_with_the_policy(self, tmp_path) -> None:
        from tests.test_remote_sandbox import SERVICE_TOKEN, Harness

        harness = Harness(workspace_root=str(tmp_path))
        with harness.client() as client:
            answer = client.put(
                "/policy",
                json={"default_runtime": harness.config.default_runtime},
                headers={wire.TOKEN_HEADER: SERVICE_TOKEN},
            )

        assert answer.status_code == 200
        assert answer.json()["default_runtime"] == harness.config.default_runtime


class TestTheServiceReadsTheFile:
    def test_startup_applies_it_before_anything_is_served(self, tmp_path) -> None:
        """So the first session is held to the ceilings the file names, rather than
        to the environment's for one whole cleanup interval."""
        from tests.test_remote_sandbox import Harness

        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"mem_limit": "3g"}))
        harness = Harness(workspace_root=str(tmp_path), policy_overrides=str(path))

        with harness.client():
            assert harness.config.mem_limit == "3g"

    @pytest.mark.anyio
    async def test_the_sweep_loop_notices_a_later_edit(self, tmp_path) -> None:
        """An edit after startup is the case the watcher exists for - and it is
        applied before the sweep, so a ceiling raised in the file is in force for
        that pass rather than the next one."""
        import asyncio
        import os

        from tests.test_remote_sandbox import Harness, _wait_until

        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"mem_limit": "1g"}))
        harness = Harness(
            workspace_root=str(tmp_path), policy_overrides=str(path), cleanup_interval=0
        )
        service = harness.app.state.service
        service.startup()
        try:
            assert harness.config.mem_limit == "1g"

            path.write_text(json.dumps({"mem_limit": "5g"}))
            stamp = path.stat().st_mtime + 10
            os.utime(path, (stamp, stamp))

            task = asyncio.create_task(service._sweep_loop())
            assert await _wait_until(lambda: harness.config.mem_limit == "5g")
            task.cancel()
        finally:
            await service.shutdown()
