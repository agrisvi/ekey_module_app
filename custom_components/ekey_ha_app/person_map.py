"""Person ↔ app-user mapping: one store owner, one migration, one reconcile.

Two things happen in this module.

**It becomes the single owner of the store.** The person→APID map used to be read
and written through a fresh ``Store`` object constructed at each of seven call
sites, with no caching and no coordination — which is also why a bare
``ekey_ha_storage_updated`` event had to be invented to stop a selector rebuilding
from stale data. One owner here removes that class of bug and makes bumping the
schema version possible at all.

**It moves authority to the backend.** The backend's ``users.json`` is the record;
Home Assistant keeps only an annotation. The link lives *on the user object* as
``ha_person``:

    {"id": "…", "username": "Jane Doe", "ha_person": "person.jane",
     "fingers": [{"apid": "…", "finger": 1}]}

That is safe with no firmware change: ``PUT /app/v1/users`` validates only that
the top level is an array and re-emits it through cJSON, so keys it does not know
survive the round trip, and the rule engine reads nothing but ``username`` and
``fingers[]``. Keeping the link there rather than in HA storage means it survives
a Home Assistant reinstall and is visible in the device's own admin page.

**The v1 data is never deleted.** It is moved verbatim under ``legacy`` and left
there forever. That is the "cannot lose mappings" guarantee: if a reconcile goes
wrong, ``.storage/ekey_ha_app.person_fingerprints`` still holds the original and
an administrator can rebuild by hand.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

# Underscore-prefixed because hass.data[DOMAIN] is keyed by config-entry id for
# everything else, and code that counts entries filters these out (see
# _entry_buckets in __init__.py).
_STORE_CACHE_KEY = "_person_store"


class EkeyPersonStore(Store):
    """The person-link store, with the v1 → v2 migration.

    ``Store`` has no ``migrate_func`` argument — migration is a subclass hook —
    so this exists purely to own :meth:`_async_migrate_func`.
    """

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: Any
    ) -> dict[str, Any]:
        """v1 → v2: keep the old map verbatim under ``legacy``."""
        if old_major_version == 1:
            _LOGGER.info(
                "Migrating ekey person store v1 → v2; the v1 map is preserved under 'legacy'"
            )
            return {"legacy": old_data or {}, "migrated_to_backend": {}}
        # A newer store on older code: refuse rather than silently discard.
        raise NotImplementedError


def async_get_store(hass: HomeAssistant) -> EkeyPersonStore:
    """Return the one store instance for this HA run."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get(_STORE_CACHE_KEY)
    if store is None:
        store = EkeyPersonStore(hass, STORAGE_VERSION, STORAGE_KEY)
        domain_data[_STORE_CACHE_KEY] = store
    return store


async def async_load(hass: HomeAssistant) -> dict[str, Any]:
    """Load the v2 payload, normalising a missing or empty store."""
    data = await async_get_store(hass).async_load()
    if not isinstance(data, dict):
        return {"legacy": {}, "migrated_to_backend": {}}
    data.setdefault("legacy", {})
    data.setdefault("migrated_to_backend", {})
    return data


async def async_save(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Persist the v2 payload."""
    await async_get_store(hass).async_save(data)


# --------------------------------------------------------------------- reading


def user_person(user: dict[str, Any]) -> str | None:
    """The ``person.*`` entity linked to this app user, if any."""
    value = user.get("ha_person")
    return value if isinstance(value, str) and value.startswith("person.") else None


def find_user_by_apid(users: list[dict[str, Any]], apid: str) -> dict[str, Any] | None:
    """The app user holding ``apid``, or ``None``.

    The APID is the join key everywhere — it is what the sensor reports on a
    recognition and the only identifier both sides agree on.
    """
    for user in users:
        for finger in user.get("fingers") or []:
            if isinstance(finger, dict) and finger.get("apid") == apid:
                return user
    return None


def as_person_map(users: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Render backend users into the legacy ``{person_id: {"fingerprints": …}}`` shape.

    The existing sensor, select and button platforms all speak this shape. Giving
    them a rendered view — rather than rewriting three platforms in this phase —
    makes the backend authoritative without touching behaviour that the shipped
    blueprints depend on.
    """
    result: dict[str, dict[str, Any]] = {}
    for user in users:
        person_id = user_person(user)
        if not person_id:
            continue
        fingerprints = result.setdefault(person_id, {"fingerprints": {}})["fingerprints"]
        for finger in user.get("fingers") or []:
            if not isinstance(finger, dict):
                continue
            apid = finger.get("apid")
            slot = finger.get("finger")
            if isinstance(apid, str) and isinstance(slot, int):
                fingerprints[str(slot)] = apid
    return result


async def async_person_map(hass: HomeAssistant, entry_id: str | None = None) -> dict[str, Any]:
    """The effective person map: from the backend when known, else from ``legacy``.

    Falling back to ``legacy`` matters during an outage and before the first
    reconcile — otherwise an unreachable backend would make every enrolled
    fingerprint read as "Unknown" in the logbook.
    """
    users = _cached_users(hass, entry_id)
    if users is not None:
        return as_person_map(users)
    data = await async_load(hass)
    legacy = data.get("legacy")
    return legacy if isinstance(legacy, dict) else {}


def _cached_users(hass: HomeAssistant, entry_id: str | None) -> list[dict[str, Any]] | None:
    """Backend users from whichever app coordinator has them, or ``None``."""
    domain_data = hass.data.get(DOMAIN) or {}
    entry_ids = [entry_id] if entry_id else list(domain_data.keys())
    for candidate in entry_ids:
        bucket = domain_data.get(candidate)
        if not isinstance(bucket, dict):
            continue
        coordinator = bucket.get("app_coordinator")
        data = getattr(coordinator, "data", None)
        if isinstance(data, dict) and isinstance(data.get("users"), list):
            return data["users"]
    return None


# ------------------------------------------------------------------- reconcile


@dataclass
class ReconcilePlan:
    """What a reconcile would do — computed without touching HA or the network."""

    users: list[dict[str, Any]]
    created: list[str] = field(default_factory=list)
    linked: list[tuple[str, str]] = field(default_factory=list)
    attached: list[tuple[str, int, str]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """True when the plan would actually write something."""
        return bool(self.created or self.linked or self.attached)


def build_reconcile_plan(
    legacy: dict[str, Any],
    backend_users: list[dict[str, Any]],
    person_names: dict[str, str],
    *,
    new_id: Any = None,
) -> ReconcilePlan:
    """Fold the legacy person→APID map into the backend's user list.

    Pure: no HA, no I/O, no clock — which is what makes it directly testable and
    what makes running it twice provably a no-op.

    Rules, in order of authority:

    1. **An APID already on a backend user wins.** The sensor and the backend
       already agree about it; the only thing possibly missing is the person link.
    2. **One person ↔ at most one app user.** A second candidate for a person that
       is already linked elsewhere is a conflict, not a merge.
    3. **An occupied finger slot is never overwritten.** Something the sensor
       knows about lives there.

    Conflicts are reported and *nothing is written for them*. Silently merging two
    people's fingerprints is the one outcome worth stopping for.
    """
    users = [dict(user) for user in backend_users]
    for user in users:
        user["fingers"] = [dict(f) for f in (user.get("fingers") or []) if isinstance(f, dict)]

    plan = ReconcilePlan(users=users)
    make_id = new_id or (lambda: str(uuid.uuid4()))

    # person_id -> user, for rule 2. Built once from the incoming state and kept
    # current as users are created.
    linked_person: dict[str, dict[str, Any]] = {}
    for user in users:
        person_id = user_person(user)
        if person_id and person_id not in linked_person:
            linked_person[person_id] = user

    for person_id in sorted(legacy):
        entry = legacy.get(person_id)
        if not isinstance(entry, dict):
            continue
        fingerprints = entry.get("fingerprints")
        if not isinstance(fingerprints, dict):
            continue

        display = person_names.get(person_id) or person_id.removeprefix("person.")

        for slot_text in sorted(fingerprints, key=lambda s: str(s)):
            apid = fingerprints.get(slot_text)
            if not isinstance(apid, str) or not apid:
                continue
            try:
                slot = int(slot_text)
            except (TypeError, ValueError):
                plan.conflicts.append(
                    f"{display}: finger slot {slot_text!r} is not a number — left in 'legacy'"
                )
                continue

            # Rule 1 — the backend already knows this fingerprint.
            owner = find_user_by_apid(users, apid)
            if owner is not None:
                existing_link = user_person(owner)
                if existing_link is None:
                    other = linked_person.get(person_id)
                    if other is not None and other is not owner:
                        plan.conflicts.append(
                            f"{display} is already linked to user "
                            f"{other.get('username')!r}, but fingerprint {apid[:8]}… "
                            f"belongs to {owner.get('username')!r} — not linked"
                        )
                        continue
                    owner["ha_person"] = person_id
                    linked_person[person_id] = owner
                    plan.linked.append((str(owner.get("username")), person_id))
                elif existing_link != person_id:
                    plan.conflicts.append(
                        f"fingerprint {apid[:8]}… is on user {owner.get('username')!r} "
                        f"linked to {existing_link}, but the old map says {person_id} "
                        f"— left alone"
                    )
                continue

            # Rule 2 — find or create the user this person maps to.
            target = linked_person.get(person_id)
            if target is None:
                target = next(
                    (u for u in users if u.get("username") == display and not user_person(u)),
                    None,
                )
                if target is not None:
                    target["ha_person"] = person_id
                    plan.linked.append((display, person_id))
                else:
                    target = {
                        "id": make_id(),
                        "username": display,
                        "ha_person": person_id,
                        "fingers": [],
                    }
                    users.append(target)
                    plan.created.append(display)
                linked_person[person_id] = target

            # Rule 3 — never overwrite an occupied slot.
            occupied = next(
                (f for f in target["fingers"] if f.get("finger") == slot), None
            )
            if occupied is not None:
                plan.conflicts.append(
                    f"{display}: finger {slot} already holds {str(occupied.get('apid'))[:8]}… "
                    f"— {apid[:8]}… not attached"
                )
                continue

            # `enrolled_at` is deliberately omitted: we do not know when this was
            # enrolled, and inventing a timestamp would be worse than having none.
            target["fingers"].append({"apid": apid, "finger": slot})
            plan.attached.append((display, slot, apid))

    return plan


@dataclass
class ReconcileReport:
    """Outcome of :func:`async_reconcile`, for logs and repair issues."""

    ran: bool = False
    skipped_already_done: bool = False
    created: list[str] = field(default_factory=list)
    linked: list[tuple[str, str]] = field(default_factory=list)
    attached: list[tuple[str, int, str]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


async def async_reconcile(
    hass: HomeAssistant,
    client,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> ReconcileReport:
    """Migrate the legacy map into the backend once per scanner.

    Guarded by ``migrated_to_backend[scanner_id]`` so a restart does not redo it,
    and idempotent even if that guard is lost — :func:`build_reconcile_plan`
    produces an empty plan when everything is already in place.
    """
    data = await async_load(hass)
    legacy = data.get("legacy") or {}
    scanner_id = client.conn.scanner_id
    report = ReconcileReport()

    if not legacy:
        return report
    if not force and not dry_run and scanner_id in data.get("migrated_to_backend", {}):
        report.skipped_already_done = True
        return report

    backend_users = await client.async_get_users()

    person_names: dict[str, str] = {}
    for person_id in legacy:
        state = hass.states.get(person_id)
        if state is not None:
            person_names[person_id] = state.attributes.get("friendly_name", person_id)

    plan = build_reconcile_plan(legacy, backend_users, person_names)
    report.created = plan.created
    report.linked = plan.linked
    report.attached = plan.attached
    report.conflicts = plan.conflicts

    if dry_run:
        return report

    if plan.changed:
        await client.async_put_users(plan.users)
        _LOGGER.info(
            "Reconciled the legacy person map into %s: %d user(s) created, "
            "%d link(s), %d fingerprint(s) attached",
            scanner_id,
            len(plan.created),
            len(plan.linked),
            len(plan.attached),
        )
    report.ran = True

    data.setdefault("migrated_to_backend", {})[scanner_id] = {
        "created": len(plan.created),
        "linked": len(plan.linked),
        "attached": len(plan.attached),
        "conflicts": len(plan.conflicts),
    }
    await async_save(hass, data)

    if plan.conflicts:
        _LOGGER.warning(
            "Reconcile left %d mapping(s) untouched for %s: %s",
            len(plan.conflicts),
            scanner_id,
            "; ".join(plan.conflicts),
        )
    return report
