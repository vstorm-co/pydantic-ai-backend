"""Disk-backed profiles keep one default context behind exact generation IDs."""

import pytest

from pydantic_ai_backends.remote._profile_cdp import ProfileCDP


def test_profile_context_is_stable_only_inside_one_generation():
    profile = ProfileCDP("one")
    first = profile.request({"id": 1, "method": "Target.createBrowserContext"})
    assert first.forward is None
    assert first.response == {"id": 1, "result": {"browserContextId": "profile-one"}}
    assert profile.request({"id": 2, "method": "Target.getBrowserContexts"}).response == {
        "id": 2,
        "result": {"browserContextIds": ["profile-one"]},
    }
    resumed = ProfileCDP("two")
    refused = resumed.request(
        {
            "id": 3,
            "method": "Target.createTarget",
            "params": {"browserContextId": "profile-one", "url": "about:blank"},
        }
    )
    assert refused.forward is None
    assert "error" in refused.response


@pytest.mark.parametrize("identifier", [None, True, "one"])
def test_invalid_command_identity_is_refused(identifier):
    with pytest.raises(ValueError, match="integer"):
        ProfileCDP("one").request({"id": identifier})


@pytest.mark.parametrize("params", [None, [], "invalid"])
def test_nonobject_parameters_are_refused(params):
    assert "error" in ProfileCDP("one").request({"id": 1, "params": params}).response


@pytest.mark.parametrize("field", ["proxyServer", "proxyBypassList"])
def test_profile_creation_cannot_override_egress(field):
    decision = ProfileCDP("one").request(
        {"id": 1, "method": "Target.createBrowserContext", "params": {field: "elsewhere"}}
    )
    assert decision.forward is None
    assert "error" in decision.response


def test_disposal_cannot_delete_the_shared_profile_or_other_tabs():
    result = ProfileCDP("one").request(
        {
            "id": 1,
            "method": "Target.disposeBrowserContext",
            "params": {"browserContextId": "profile-one"},
        }
    )
    assert result.forward is None
    assert "error" in result.response


def test_logical_context_parameter_routes_to_the_default_context():
    profile = ProfileCDP("one")
    assert profile.request(
        {
            "id": 1,
            "method": "Target.createTarget",
            "params": {"browserContextId": "profile-one", "url": "about:blank"},
        }
    ).forward == {"id": 1, "method": "Target.createTarget", "params": {"url": "about:blank"}}
    ordinary = {"id": 2, "method": "Page.reload"}
    assert profile.request(ordinary).forward == ordinary


@pytest.mark.parametrize("context", [None, "", "native-default"])
def test_target_replies_and_events_expose_current_generation(context):
    profile = ProfileCDP("one", "native-default")
    item = {"targetId": "page", "browserContextId": context}
    reply = profile.response(
        {"id": 1, "result": {"targetInfo": item, "targetInfos": [item, 12], "other": True}}
    )
    assert reply["result"]["targetInfo"]["browserContextId"] == "profile-one"
    assert reply["result"]["targetInfos"][0]["browserContextId"] == "profile-one"
    assert reply["result"]["targetInfos"][1] == 12
    assert (
        profile.response({"method": "Target.targetCreated", "params": {"targetInfo": item}})[
            "params"
        ]["targetInfo"]["browserContextId"]
        == "profile-one"
    )


def test_unexpected_private_target_stays_foreign():
    item = {"targetId": "foreign", "browserContextId": "private-other"}
    assert ProfileCDP("one").response({"result": {"targetInfo": item}}) == {
        "result": {"targetInfo": item}
    }


def test_websites_returning_target_named_json_are_not_rewritten():
    profile = ProfileCDP("one")
    value = {"targetInfo": {"customer": "alice"}, "targetInfos": [{"private": True}]}
    payload = {"id": 1, "result": {"result": {"type": "object", "value": value}}}
    assert profile.response(payload) == payload
    event = {"method": "Runtime.bindingCalled", "params": {"targetInfo": value}}
    assert profile.response(event) == event
