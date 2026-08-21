"""What this particular backend can do.

Three kinds of backend exist in the field and they are not interchangeable:

* an **ESP32** with the app layer and, from this work onward, a capabilities
  endpoint;
* an **ESP32 or daemon** with the app layer but no capabilities endpoint (any
  firmware built before it landed);
* a **plain daemon** with no app layer at all — ``/app/v1/*`` answers 404.

The panel and the entities must behave sensibly on all three, and "sensibly" does
not mean guessing. So detection has three outcomes rather than a boolean, and the
one thing this module never does is claim a capability it has not been told about:
an action type whose support is *unknown* is not offered, exactly as if it were
unsupported. Quietly offering a KNX action to a backend that cannot send one moves
the failure to 3 a.m., which is the whole reason the endpoint exists.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .api import EkeyApiError, EkeyAppClient, EkeyAuthError

_LOGGER = logging.getLogger(__name__)

SOURCE_ENDPOINT = "capabilities"
SOURCE_PROBE = "probe"
SOURCE_ABSENT = "absent"
SOURCE_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Capabilities:
    """A backend's advertised abilities, or what could be inferred about them."""

    source: str
    platform: str | None = None
    core_version: str | None = None
    action_types: dict[str, bool] = field(default_factory=dict)
    action_reasons: dict[str, str] = field(default_factory=dict)
    trigger_kinds: list[str] = field(default_factory=list)
    template_tokens: list[str] = field(default_factory=list)
    features: dict[str, bool] = field(default_factory=dict)

    @property
    def has_app_api(self) -> bool:
        """True when the backend serves ``/app/v1`` at all."""
        return self.source in (SOURCE_ENDPOINT, SOURCE_PROBE)

    @property
    def known(self) -> bool:
        """True when the backend told us its capabilities rather than us guessing."""
        return self.source == SOURCE_ENDPOINT

    def supports_action(self, action_type: str) -> bool:
        """Whether an action of this type can actually run on this backend.

        Unknown counts as unsupported — see the module docstring.
        """
        return bool(self.action_types.get(action_type, False))

    def action_reason(self, action_type: str) -> str | None:
        """Why an action type is unsupported, when the backend explained itself."""
        if self.supports_action(action_type):
            return None
        if action_type in self.action_reasons:
            return self.action_reasons[action_type]
        if not self.known:
            return "this backend does not report its supported action types"
        return "not supported by this backend"

    def has_feature(self, name: str) -> bool:
        """Whether a named feature (``users``, ``event_log``, …) is available."""
        return bool(self.features.get(name, False))

    def as_dict(self) -> dict:
        """Serialisable form, for the panel and for diagnostics."""
        return {
            "source": self.source,
            "has_app_api": self.has_app_api,
            "known": self.known,
            "platform": self.platform,
            "core_version": self.core_version,
            "action_types": self.action_types,
            "action_reasons": self.action_reasons,
            "trigger_kinds": self.trigger_kinds,
            "template_tokens": self.template_tokens,
            "features": self.features,
        }


def _parse(payload: dict) -> Capabilities:
    """Turn a ``/app/v1/capabilities`` body into a :class:`Capabilities`.

    Accepts both shapes the endpoint may present ``action_types`` in — a list of
    objects carrying a ``reason``, or a bare list of names — because the richer
    form is what the daemon emits and the simpler one is easier to hand-write.
    """
    action_types: dict[str, bool] = {}
    action_reasons: dict[str, str] = {}
    for item in payload.get("action_types") or []:
        if isinstance(item, str):
            action_types[item] = True
        elif isinstance(item, dict):
            name = item.get("type")
            if not isinstance(name, str):
                continue
            action_types[name] = bool(item.get("supported", True))
            reason = item.get("reason")
            if isinstance(reason, str) and reason:
                action_reasons[name] = reason

    features = payload.get("features")
    if not isinstance(features, dict):
        features = {}
    # A backend that answers this endpoint has the user document by definition;
    # older ones may not list it explicitly.
    features = {str(k): bool(v) for k, v in features.items()}
    features.setdefault("users", True)

    return Capabilities(
        source=SOURCE_ENDPOINT,
        platform=payload.get("platform") if isinstance(payload.get("platform"), str) else None,
        core_version=(
            payload.get("core_version")
            if isinstance(payload.get("core_version"), str)
            else None
        ),
        action_types=action_types,
        action_reasons=action_reasons,
        trigger_kinds=[t for t in (payload.get("trigger_kinds") or []) if isinstance(t, str)],
        template_tokens=[
            t for t in (payload.get("template_tokens") or []) if isinstance(t, str)
        ],
        features=features,
    )


async def async_detect(client: EkeyAppClient) -> Capabilities:
    """Ask the backend what it can do, falling back to a probe, then to nothing.

    Auth failures are *not* swallowed into "no app layer": a rotated token must
    surface as a reauth, not as "your device is too old". They are reported as
    ``unavailable`` so the caller can tell the two apart.
    """
    try:
        payload = await client.async_capabilities()
    except EkeyAuthError:
        raise
    except EkeyApiError as err:
        _LOGGER.debug("Capability endpoint unreachable on %s: %s", client.conn.scanner_id, err)
        return Capabilities(source=SOURCE_UNAVAILABLE)

    if payload is not None:
        caps = _parse(payload)
        _LOGGER.debug(
            "%s advertises platform=%s features=%s",
            client.conn.scanner_id,
            caps.platform,
            sorted(k for k, v in caps.features.items() if v),
        )
        return caps

    # No capabilities endpoint. Does it have the app layer at all?
    try:
        present = await client.async_has_app_api()
    except EkeyAuthError:
        raise
    except EkeyApiError as err:
        _LOGGER.debug("App-layer probe failed on %s: %s", client.conn.scanner_id, err)
        return Capabilities(source=SOURCE_UNAVAILABLE)

    if not present:
        _LOGGER.info(
            "%s has no app layer (/app/v1 answers 404) — user management is unavailable "
            "for this backend until its firmware or daemon is updated",
            client.conn.scanner_id,
        )
        return Capabilities(source=SOURCE_ABSENT)

    # App layer present, capabilities unknown. Users work; nothing else is claimed.
    return Capabilities(
        source=SOURCE_PROBE,
        features={"users": True},
        trigger_kinds=["match_ok", "match_nok", "touch"],
        template_tokens=["apid", "username", "result", "ts", "finger"],
    )
