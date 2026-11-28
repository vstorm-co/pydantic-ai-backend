"""Expose one durable profile through a generation-scoped CDP context identity.

Each managed profile owns a dedicated container. Chromium's default context is
the disk-backed context; incognito contexts would discard authentication storage.
The public context identity changes whenever the browser process is resumed.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue, TypeAdapter

_OBJECT = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True)
class ProfileCommand:
    forward: dict[str, JsonValue] | None
    response: dict[str, JsonValue] | None = None


class ProfileCDP:
    def __init__(self, generation: str, default_context_id: str | None = None) -> None:
        self.context_id = f"profile-{generation}"
        self.default_context_id = default_context_id

    def request(self, payload: dict[str, object]) -> ProfileCommand:
        message = _OBJECT.validate_python(payload)
        identifier = message.get("id")
        if not isinstance(identifier, int) or isinstance(identifier, bool):
            raise ValueError("Profile commands require an integer identity")
        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return self._error(identifier, "Invalid profile command parameters")
        context = params.get("browserContextId")
        if context is not None and context != self.context_id:
            return self._error(identifier, "The exact browser context no longer exists")
        if method == "Target.createBrowserContext":
            if params.get("proxyServer") or params.get("proxyBypassList"):
                return self._error(identifier, "The managed profile has an operator-owned proxy")
            return ProfileCommand(
                None, {"id": identifier, "result": {"browserContextId": self.context_id}}
            )
        if method == "Target.getBrowserContexts":
            return ProfileCommand(
                None, {"id": identifier, "result": {"browserContextIds": [self.context_id]}}
            )
        if method == "Target.disposeBrowserContext":
            return self._error(identifier, "Close individual tabs or explicitly end the profile")
        if context is not None:
            message["params"] = {
                key: value for key, value in params.items() if key != "browserContextId"
            }
        return ProfileCommand(message)

    @staticmethod
    def _error(identifier: int, message: str) -> ProfileCommand:
        return ProfileCommand(
            None, {"id": identifier, "error": {"code": -32000, "message": message}}
        )

    def response(self, payload: dict[str, object]) -> dict[str, JsonValue]:
        message = _OBJECT.validate_python(payload)
        result = message.get("result")
        if isinstance(result, dict):
            message["result"] = self._targets(result)
        method = message.get("method")
        params = message.get("params")
        if isinstance(method, str) and method.startswith("Target.") and isinstance(params, dict):
            message["params"] = self._targets(params)
        return message

    def _targets(self, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        result = dict(payload)
        target = payload.get("targetInfo")
        if isinstance(target, dict):
            result["targetInfo"] = self._target(target)
        targets = payload.get("targetInfos")
        if isinstance(targets, list):
            result["targetInfos"] = [
                self._target(item) if isinstance(item, dict) else item for item in targets
            ]
        return result

    def _target(self, target: dict[str, JsonValue]) -> dict[str, JsonValue]:
        # Only the dedicated browser's normal context belongs to this profile.
        # Unexpected private contexts remain distinct and fail ownership checks.
        if target.get("browserContextId") in (None, "", self.default_context_id):
            return {**target, "browserContextId": self.context_id}
        return target
