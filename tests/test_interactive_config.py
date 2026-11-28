"""Interactive admission ceilings are positive, startup-only operator policy."""

import pytest
from pydantic import ValidationError

from pydantic_ai_backends.remote.env import config_from_env
from pydantic_ai_backends.remote.server import SandboxdConfig
from pydantic_ai_backends.remote.wire import PolicyUpdate, ServicePolicy


@pytest.mark.parametrize(
    "setting",
    ["interactive_idle_timeout", "interactive_max_lifetime", "max_interactive_environments"],
)
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "30"])
def test_invalid_interactive_ceiling_is_refused(setting, value):
    with pytest.raises(ValueError, match=f"{setting} must be a positive integer"):
        SandboxdConfig(token="test-service-token", **{setting: value})


def test_environment_controls_all_interactive_admission_ceilings():
    config = config_from_env(
        {
            "SANDBOXD_TOKEN": "test-service-token",
            "SANDBOXD_INTERACTIVE_IDLE_TIMEOUT": "40",
            "SANDBOXD_INTERACTIVE_MAX_LIFETIME": "80",
            "SANDBOXD_MAX_INTERACTIVE_ENVIRONMENTS": "3",
        }
    )
    assert config.interactive_idle_timeout == 40
    assert config.interactive_max_lifetime == 80
    assert config.max_interactive_environments == 3


@pytest.mark.parametrize(
    "setting",
    ["interactive_idle_timeout", "interactive_max_lifetime", "max_interactive_environments"],
)
def test_interactive_ceilings_are_not_writable_through_policy(setting):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PolicyUpdate.model_validate({setting: 60})


def test_old_server_policy_does_not_advertise_interactive_support():
    assert ServicePolicy().interactive_environments is False
