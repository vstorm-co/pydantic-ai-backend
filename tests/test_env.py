"""Configuring `sandboxd` from the environment.

The entrypoint read four variables while `SandboxdConfig` models thirty, so a
deployment that needed any of the rest wrote Python. These tests exist at the
same altitude as that problem: they assert what a compose file produces, which
is the thing an operator actually writes and previously could not verify without
starting a container.
"""

from __future__ import annotations

import dataclasses

import pytest

from pydantic_ai_backends.remote.env import (
    SandboxdConfigError,
    bind_from_env,
    config_from_env,
)
from pydantic_ai_backends.remote.server import DEFAULT_RUNTIMES, SandboxdConfig
from pydantic_ai_backends.types import RuntimeConfig

TOKEN = {"SANDBOXD_TOKEN": "a-long-random-secret"}


def _config(**env: str) -> SandboxdConfig:
    return config_from_env({**TOKEN, **env})


class TestTheToken:
    def test_a_missing_token_names_the_variable_and_says_what_it_is_worth(self):
        with pytest.raises(SandboxdConfigError) as exc:
            config_from_env({})

        assert "SANDBOXD_TOKEN" in str(exc.value)
        assert "run commands on this host" in str(exc.value)

    def test_an_empty_token_is_missing(self):
        with pytest.raises(SandboxdConfigError):
            config_from_env({"SANDBOXD_TOKEN": ""})


class TestDefaults:
    def test_only_a_token_yields_the_shipped_policy(self):
        config = _config()

        assert dict(config.runtimes) == DEFAULT_RUNTIMES
        assert config.max_sessions == 20
        assert config.network_mode == "none"
        assert config.ui_enabled is False

    def test_every_field_the_parser_defaults_is_the_dataclass_default(self):
        """The parser reads its defaults off an instance, which `__post_init__` may rewrite.

        `default_runtime` is the field that does — an empty one becomes the
        first entry of the allowlist — and passing that resolved value back in
        pinned every deployment to the first *shipped* runtime and refused any
        custom allowlist. Nothing else may quietly join it: a second derived
        field would reintroduce the same bug somewhere new, and this is the
        assertion that notices.
        """
        parsed = _config()
        declared = {
            f.name: f.default
            for f in dataclasses.fields(SandboxdConfig)
            if f.default is not dataclasses.MISSING
        }

        derived = {
            name
            for name, default in declared.items()
            if getattr(parsed, name) != default and name != "token"
        }

        assert derived == {"default_runtime"}


class TestScalarFields:
    def test_numbers_and_flags_are_parsed(self):
        config = _config(
            SANDBOXD_MAX_SESSIONS="4",
            SANDBOXD_CPUS="1.5",
            SANDBOXD_PERSIST_CONTAINERS="yes",
            SANDBOXD_IDLE_TIMEOUT="60",
        )

        assert config.max_sessions == 4
        assert config.cpus == 1.5
        assert config.persist_containers is True
        assert config.idle_timeout == 60

    def test_an_empty_optional_turns_a_defaulted_ceiling_off(self):
        """`SANDBOXD_CPUS=` says "no hard CPU ceiling", which absent cannot say."""
        assert _config(SANDBOXD_CPUS="").cpus is None
        assert _config(SANDBOXD_MEM_LIMIT="").mem_limit is None
        assert _config().cpus == 2.0

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_spellings(self, raw: str):
        assert _config(SANDBOXD_UI_ENABLED=raw).ui_enabled is True

    @pytest.mark.parametrize("raw", ["0", "false", "No", "off"])
    def test_falsy_spellings(self, raw: str):
        assert _config(SANDBOXD_PREWARM=raw).prewarm is False

    def test_a_non_boolean_lists_what_is_accepted(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_UI_ENABLED="maybe")

        assert "SANDBOXD_UI_ENABLED" in str(exc.value)
        assert "true" in str(exc.value)

    def test_a_bad_number_names_the_variable_rather_than_raising_from_int(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_MAX_SESSIONS="lots")

        assert "SANDBOXD_MAX_SESSIONS='lots'" in str(exc.value)

    def test_a_bad_number_on_a_non_optional_field_is_reported_the_same_way(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_IDLE_TIMEOUT="soon")

        assert "SANDBOXD_IDLE_TIMEOUT" in str(exc.value)

    def test_an_empty_work_dir_is_refused_rather_than_stored(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_WORK_DIR="")

        assert "SANDBOXD_WORK_DIR" in str(exc.value)


class TestCombinationsTheServiceRefuses:
    def test_hibernation_without_a_workspace_root_is_refused_at_startup(self):
        """The setting whose absence is otherwise silent — files simply vanish."""
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_EVICT_IDLE_AFTER="600")

        assert "workspace_root" in str(exc.value)

    def test_hibernation_with_one_is_accepted(self):
        config = _config(SANDBOXD_EVICT_IDLE_AFTER="600", SANDBOXD_WORKSPACE_ROOT="/workspaces")

        assert config.evict_idle_after == 600
        assert config.workspace_root == "/workspaces"

    def test_a_default_runtime_no_entry_defines_is_refused(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_RUNTIMES="py=python:3.12-slim", SANDBOXD_DEFAULT_RUNTIME="nope")

        assert "nope" in str(exc.value)


class TestTheCompactRuntimeForm:
    def test_alias_and_image(self):
        runtimes = _config(SANDBOXD_RUNTIMES="py=python:3.12-slim").runtimes

        assert runtimes["py"].image == "python:3.12-slim"

    def test_a_custom_allowlist_defaults_to_its_own_first_entry(self):
        config = _config(SANDBOXD_RUNTIMES="py=python:3.12-slim,node=node:20-slim")

        assert config.default_runtime == "py"

    def test_an_at_sign_builds_a_named_runtime_instead_of_pulling(self):
        runtimes = _config(SANDBOXD_RUNTIMES="data=@python-datascience").runtimes

        assert runtimes["data"].runtime == "python-datascience"
        assert runtimes["data"].builds is True

    def test_modifiers_use_the_dataclass_field_names(self):
        runtimes = _config(
            SANDBOXD_RUNTIMES="data=@python-datascience;mem_limit=4g;cpus=3;pids_limit=64"
        ).runtimes

        assert runtimes["data"].mem_limit == "4g"
        assert runtimes["data"].cpus == 3.0
        assert runtimes["data"].pids_limit == 64

    def test_blank_entries_and_modifiers_are_ignored(self):
        runtimes = _config(SANDBOXD_RUNTIMES=" py=python:3.12-slim;; , ").runtimes

        assert list(runtimes) == ["py"]

    @pytest.mark.parametrize("raw", ["oops", "=python:3.12-slim", "py="])
    def test_an_entry_that_is_not_alias_equals_image_shows_an_example(self, raw: str):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_RUNTIMES=raw)

        assert "SANDBOXD_RUNTIMES" in str(exc.value)
        assert "Example:" in str(exc.value)

    def test_a_modifier_without_a_value_is_refused(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_RUNTIMES="py=python:3.12-slim;mem_limit")

        assert "field=value" in str(exc.value)

    def test_a_modifier_naming_no_such_field_is_refused(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_RUNTIMES="py=python:3.12-slim;memory=4g")

        assert "does not have" in str(exc.value)

    def test_a_non_numeric_ceiling_names_the_runtime_and_the_field(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_RUNTIMES="py=python:3.12-slim;cpus=plenty")

        assert "RUNTIMES py.cpus" in str(exc.value)

    def test_an_entry_that_is_both_an_image_and_a_build_is_refused(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_RUNTIMES="py=python:3.12-slim;runtime=coding")

        assert "exactly one of image or runtime" in str(exc.value)

    def test_a_value_naming_no_runtime_at_all_says_to_unset_it(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_RUNTIMES=" , ")

        assert "Unset it" in str(exc.value)


class TestTheJsonRuntimeForm:
    def test_an_object_of_images(self):
        runtimes = _config(SANDBOXD_RUNTIMES='{"py": "python:3.12-slim"}').runtimes

        assert runtimes["py"].image == "python:3.12-slim"

    def test_an_object_of_entries_with_ceilings(self):
        runtimes = _config(
            SANDBOXD_RUNTIMES='{"data": {"runtime": "python-datascience", "mem_limit": "4g"}}'
        ).runtimes

        assert runtimes["data"].mem_limit == "4g"

    def test_a_build_written_out_rather_than_named(self):
        """The one thing the compact form cannot express."""
        runtimes = _config(
            SANDBOXD_RUNTIMES=(
                '{"ml": {"runtime": {"name": "ml", "base_image": "python:3.12-slim", '
                '"packages": ["torch"]}, "mem_limit": "8g"}}'
            )
        ).runtimes

        built = runtimes["ml"].runtime
        assert isinstance(built, RuntimeConfig)
        assert built.packages == ["torch"]

    def test_an_at_sign_still_works_inside_json(self):
        runtimes = _config(SANDBOXD_RUNTIMES='{"data": "@python-datascience"}').runtimes

        assert runtimes["data"].builds is True

    def test_broken_json_is_reported_as_broken_json(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_RUNTIMES='{"py": ')

        assert "not valid JSON" in str(exc.value)

    @pytest.mark.parametrize("raw", ['{"py": ""}', '{"py": "@"}'])
    def test_an_entry_naming_nothing_is_refused(self, raw: str):
        """`SandboxRuntime` accepts an empty image — exactly one field is set —
        so a runtime that pulls an image with no name would have started and
        failed on first use."""
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_RUNTIMES=raw)

        assert "names no image or runtime" in str(exc.value)

    def test_an_array_is_told_the_shape_is_wrong_rather_than_the_syntax(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_RUNTIMES='["python:3.12-slim"]')

        assert "must be an object of aliases" in str(exc.value)

    def test_an_entry_that_is_neither_a_string_nor_an_object_is_refused(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_RUNTIMES='{"py": 3}')

        assert "image string or an object" in str(exc.value)

    def test_an_invalid_nested_build_names_the_alias(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_RUNTIMES='{"ml": {"runtime": {"packages": "not-a-list"}}}')

        assert "'ml'" in str(exc.value)

    def test_an_empty_json_object_names_no_runtime(self):
        with pytest.raises(SandboxdConfigError) as exc:
            _config(SANDBOXD_RUNTIMES="{}")

        assert "names no runtime" in str(exc.value)


class TestBind:
    def test_defaults_to_loopback(self):
        assert bind_from_env({}) == ("127.0.0.1", 8080)

    def test_host_and_port_are_read(self):
        assert bind_from_env({"SANDBOXD_HOST": "0.0.0.0", "SANDBOXD_PORT": "8420"}) == (
            "0.0.0.0",
            8420,
        )

    def test_a_bad_port_names_the_variable(self):
        with pytest.raises(SandboxdConfigError) as exc:
            bind_from_env({"SANDBOXD_PORT": "http"})

        assert "SANDBOXD_PORT" in str(exc.value)
