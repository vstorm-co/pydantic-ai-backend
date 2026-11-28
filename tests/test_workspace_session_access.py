"""Session credentials are explicit, scoped and observational."""

from dataclasses import FrozenInstanceError

import pytest
from pydantic_ai.workspaces import WorkspaceUnavailableError

from pydantic_ai_backends.workspaces import SandboxdWorkspaceBackend
from pydantic_ai_backends.workspaces._sandboxd import _Session


def test_access_never_opens_a_missing_workspace():
    backend = SandboxdWorkspaceBackend("http://sandboxd", token="service-secret")
    with pytest.raises(WorkspaceUnavailableError, match="Open the sandbox workspace"):
        backend.session_access()
    assert backend.ref is None


def test_access_returns_only_the_existing_session_credential():
    backend = SandboxdWorkspaceBackend("http://sandboxd", token="service-secret")
    backend._session = _Session("known-session", "limited-secret", 60)
    access = backend.session_access()
    assert access.session_id == "known-session"
    assert access.token == "limited-secret"
    assert "limited-secret" not in repr(access)
    assert "service-secret" not in repr(access)
    assert backend.session_access() == access
    with pytest.raises(FrozenInstanceError):
        access.token = "replacement"
