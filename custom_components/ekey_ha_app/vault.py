"""The fingerprint database — Home Assistant's own copy of every template.

Why this exists at all: a fingerprint template cannot be re-derived. If a sensor is
replaced or factory-reset, every person on it comes back and presents a finger
again — unless something kept a copy. Nothing did, on either side: the device's own
admin page has no export, and this integration held only the person→APID map.

What makes a *central* copy possible rather than merely a backup is that the APID
lives inside the template's plaintext header (see :mod:`.templates`), so a template
written to a second scanner keeps its identity. One physical finger therefore has
one APID across the whole fleet, and that APID is the primary key here.

This is a SECOND store rather than another key inside the person store, because the
two are nothing alike: the person map is a few hundred bytes read on every
recognition, while this holds ~14.6 kB of hex per finger and is touched only when a
fingerprint is adopted, restored or enrolled.

Two rules the rest of the integration depends on:

* **Every mutation returns a new dict.** The store is constructed with
  ``serialize_in_event_loop=False`` so that encoding ~1.5 MB of JSON happens off
  the event loop, and that is only safe while nothing mutates the payload during
  serialisation. So no function here edits a dict in place.
* **The database is never the source of access.** Deleting from it takes nobody's
  finger off a door; the scanners keep working. That is what makes "clean storage"
  survivable and why a record may only be dropped once the sensors agree.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, EVENT_VAULT_CHANGED, VAULT_STORAGE_KEY, VAULT_STORAGE_VERSION
from .templates import DEFAULT_DOMAIN_ID, TemplateInfo

_LOGGER = logging.getLogger(__name__)

# Underscore-prefixed: hass.data[DOMAIN] is keyed by config-entry id for
# everything else, and _entry_buckets() in __init__.py filters these out.
_VAULT_CACHE_KEY = "_vault"

# A person with no linked HA person is keyed by name. The prefix keeps the two
# kinds of key from ever colliding — an entity id always contains a dot, and a
# username never reaches this side of the prefix.
NAME_KEY_PREFIX = "name:"


class EkeyVaultStore(Store):
    """The template store, with no migration to perform yet.

    ``Store`` takes no ``migrate_func`` argument — migration is a subclass hook —
    so this class exists to own :meth:`_async_migrate_func`, exactly as
    :class:`~.person_map.EkeyPersonStore` does.
    """

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: Any
    ) -> dict[str, Any]:
        """There is no version 0. A newer store on older code must not be discarded.

        Raising here means Home Assistant leaves the file alone instead of
        overwriting it with an empty database — and the file holds the only copy of
        data nobody can regenerate.
        """
        raise NotImplementedError(
            f"cannot migrate the ekey fingerprint database from version "
            f"{old_major_version}.{old_minor_version}; this Home Assistant is older "
            "than the file it found. The file has been left untouched."
        )


def async_get_store(hass: HomeAssistant) -> EkeyVaultStore:
    """Return the one store instance for this HA run.

    ``private=True`` because the file holds biometric templates: it is the one
    thing in this integration's storage that is worth stricter permissions than
    the rest of the config directory.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get(_VAULT_CACHE_KEY)
    if store is None:
        store = EkeyVaultStore(
            hass,
            VAULT_STORAGE_VERSION,
            VAULT_STORAGE_KEY,
            private=True,
            atomic_writes=True,
            # ~14.6 kB of hex per fingerprint means a hundred of them is ~1.5 MB of
            # JSON. Encoding that on the event loop is a visible stall, and every
            # mutation here builds a fresh dict, so nothing can change underneath
            # the encoder.
            serialize_in_event_loop=False,
        )
        domain_data[_VAULT_CACHE_KEY] = store
    return store


def empty_vault() -> dict[str, Any]:
    """A database with nothing in it."""
    return {"version": VAULT_STORAGE_VERSION, "records": {}, "people": {}, "changed": 0}


def normalise(data: Any) -> dict[str, Any]:
    """Coerce whatever was on disk into the shape the rest of this module expects.

    Tolerant on purpose: a hand-edited file, or one written by a future version
    that added keys, must not take the database out of service. Records that are
    not dicts are dropped rather than carried, because every reader here would
    then have to guard against them individually.
    """
    if not isinstance(data, dict):
        return empty_vault()

    records = data.get("records")
    people = data.get("people")
    clean = empty_vault()
    if isinstance(records, dict):
        clean["records"] = {
            str(apid): record
            for apid, record in records.items()
            if isinstance(record, dict) and isinstance(apid, str) and apid
        }
    if isinstance(people, dict):
        clean["people"] = {
            str(key): person for key, person in people.items() if isinstance(person, dict)
        }
    changed = data.get("changed")
    clean["changed"] = changed if isinstance(changed, (int, float)) else 0
    return clean


# ------------------------------------------------------------------- identity


def person_key(username: str | None, ha_person: str | None = None) -> str:
    """The key that joins one person's records across scanners.

    A linked Home Assistant person wins, because that is where identity already
    lives in this system — the logbook, the automations and the person→APID map all
    speak it, and it survives a rename on any individual scanner.

    Without a link there is nothing to join on but the name, so the name is
    case-folded and stripped to at least survive inconsistent typing. That is a
    real weakness and it is the reason the UI nudges towards linking a person: two
    different people sharing a name would merge here, and a rename on one scanner
    would split one person into two.
    """
    if isinstance(ha_person, str) and ha_person.startswith("person."):
        return ha_person
    return NAME_KEY_PREFIX + (username or "").strip().casefold()


def is_person_link(key: str) -> bool:
    """Whether this key came from a linked HA person rather than from a name."""
    return isinstance(key, str) and not key.startswith(NAME_KEY_PREFIX)


# -------------------------------------------------------------------- records


def _now() -> int:
    return int(time.time())


def put_record(
    data: dict[str, Any],
    *,
    apid: str,
    username: str | None,
    finger: int,
    ha_person: str | None = None,
    template: TemplateInfo | None = None,
    domain_id: str | None = None,
    dev_variant: int | None = None,
    dev_sub_variant: int | None = None,
    source_entry_id: str | None = None,
    source_scanner_id: str | None = None,
    source_prod_sn: str | None = None,
) -> dict[str, Any]:
    """Store or refresh one fingerprint. Returns a NEW database dict.

    Refreshing an existing APID keeps its ``captured_at`` and only moves
    ``updated_at``: the template is the same finger read again, not a new one.

    A *different* APID for a person's finger slot that is already occupied is the
    re-enrolment case, and it is marked rather than merged — the old template still
    works on whatever scanners hold it, so silently replacing the record would
    leave a working fingerprint that this database no longer knows about. The old
    record gets ``superseded_by`` and the UI can offer to delete it everywhere.
    """
    key = person_key(username, ha_person)
    now = _now()
    records = dict(data.get("records") or {})
    people = dict(data.get("people") or {})

    apid = str(apid).strip().lower()
    previous = records.get(apid)

    for other_apid, other in list(records.items()):
        if (
            other_apid != apid
            and other.get("person_key") == key
            and other.get("finger") == finger
            and not other.get("superseded_by")
        ):
            records[other_apid] = {**other, "superseded_by": apid, "updated_at": now}

    record = {
        "person_key": key,
        "username": (username or "").strip() or None,
        "finger": int(finger),
        "template": template.hex if template else (previous or {}).get("template"),
        "domain_id": (
            (template.domain_id if template else None)
            or domain_id
            or (previous or {}).get("domain_id")
            or DEFAULT_DOMAIN_ID
        ),
        "tif_len": template.tif_len if template else (previous or {}).get("tif_len"),
        "sha256": template.sha256 if template else (previous or {}).get("sha256"),
        "dev_variant": (
            dev_variant if dev_variant is not None else (previous or {}).get("dev_variant")
        ),
        "dev_sub_variant": (
            dev_sub_variant
            if dev_sub_variant is not None
            else (previous or {}).get("dev_sub_variant")
        ),
        "source": {
            "entry_id": source_entry_id or (previous or {}).get("source", {}).get("entry_id"),
            "scanner_id": source_scanner_id
            or (previous or {}).get("source", {}).get("scanner_id"),
            "prod_sn": source_prod_sn or (previous or {}).get("source", {}).get("prod_sn"),
        },
        "captured_at": (previous or {}).get("captured_at") or now,
        "updated_at": now,
        "superseded_by": (previous or {}).get("superseded_by"),
    }

    records[apid] = record
    people[key] = {
        "ha_person": ha_person if is_person_link(key) else None,
        "name": record["username"] or people.get(key, {}).get("name") or key,
    }
    return {**data, "records": records, "people": people, "changed": now}


def drop_record(data: dict[str, Any], apid: str) -> dict[str, Any]:
    """Remove one fingerprint. Returns a NEW database dict.

    Callers must have confirmed the sensors no longer hold it — a fingerprint that
    still answers on a scanner still opens the door, and must never vanish from
    this list. That check lives in the job layer, not here, because only it can
    talk to the scanners.
    """
    records = dict(data.get("records") or {})
    records.pop(str(apid).strip().lower(), None)
    people = {
        key: person
        for key, person in (data.get("people") or {}).items()
        if any(r.get("person_key") == key for r in records.values())
    }
    return {**data, "records": records, "people": people, "changed": _now()}


def stored_apids(data: dict[str, Any]) -> set[str]:
    """Every APID the database knows about, superseded ones included."""
    return set((data.get("records") or {}).keys())


def pushable(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Records that could actually be written to a scanner.

    A record with no template is metadata only: it can name a finger but cannot
    repair anything, so it must never be counted towards "n missing, push them".
    """
    return {
        apid: record
        for apid, record in (data.get("records") or {}).items()
        if record.get("template")
    }


def total_bytes(data: dict[str, Any]) -> int:
    """Size of the stored templates, for the "0.4 MB" line in the UI."""
    return sum(
        len(record["template"]) // 2
        for record in (data.get("records") or {}).values()
        if isinstance(record.get("template"), str)
    )


def build_records_view(data: dict[str, Any]) -> dict[str, Any]:
    """The database half of what the panel renders — **never the templates**.

    The hex is deliberately absent: it is ~14.6 kB per finger, the panel has no use
    for it, and a payload that carries biometric data to a browser for display is a
    copy nobody asked for. ``has_template`` is what the panel needs to know.
    """
    records = data.get("records") or {}
    people = data.get("people") or {}

    by_person: dict[str, list[dict[str, Any]]] = {}
    for apid, record in records.items():
        by_person.setdefault(record.get("person_key") or "", []).append(
            {
                "apid": apid,
                "finger": record.get("finger"),
                "has_template": bool(record.get("template")),
                "domain_id": record.get("domain_id") or DEFAULT_DOMAIN_ID,
                "dev_variant": record.get("dev_variant"),
                "captured_at": record.get("captured_at"),
                "updated_at": record.get("updated_at"),
                "source_entry_id": (record.get("source") or {}).get("entry_id"),
                "superseded_by": record.get("superseded_by"),
            }
        )

    users = []
    for key, fingers in by_person.items():
        person = people.get(key) or {}
        users.append(
            {
                "key": key,
                "username": person.get("name") or key.removeprefix(NAME_KEY_PREFIX),
                "ha_person": person.get("ha_person"),
                "fingers": sorted(fingers, key=lambda f: (f.get("finger") or 0, f["apid"])),
            }
        )
    users.sort(key=lambda u: (u["username"] or "").casefold())

    return {
        "version": data.get("version", VAULT_STORAGE_VERSION),
        "record_count": len(records),
        "user_count": len(users),
        "bytes": total_bytes(data),
        "changed": data.get("changed") or 0,
        "users": users,
    }


# ---------------------------------------------------------------- the instance


class EkeyVault:
    """The loaded database, and the only thing that writes it.

    Holds the cached payload so the panel's reads cost nothing, and funnels every
    mutation through :meth:`_async_commit` so that saving and announcing the change
    cannot drift apart — the same "write, then announce" rule the app coordinator
    follows.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = async_get_store(hass)
        self._data: dict[str, Any] = empty_vault()
        self._loaded = False

    @property
    def data(self) -> dict[str, Any]:
        """The cached payload. Treat as read-only; mutations build a new dict."""
        return self._data

    async def async_load(self) -> dict[str, Any]:
        """Read the store once. Safe to call repeatedly."""
        if not self._loaded:
            self._data = normalise(await self._store.async_load())
            self._loaded = True
            _LOGGER.debug(
                "Fingerprint database loaded: %d record(s), %d byte(s) of templates",
                len(self._data.get("records") or {}),
                total_bytes(self._data),
            )
        return self._data

    async def _async_commit(self, data: dict[str, Any]) -> dict[str, Any]:
        """Persist a new payload and tell every open panel."""
        self._data = data
        self._loaded = True
        await self._store.async_save(data)
        self.hass.bus.async_fire(EVENT_VAULT_CHANGED, {"entry_id": None})
        return data

    async def async_put(self, **kwargs: Any) -> dict[str, Any]:
        """Store or refresh one fingerprint. See :func:`put_record`."""
        await self.async_load()
        return await self._async_commit(put_record(self._data, **kwargs))

    async def async_drop(self, apid: str) -> dict[str, Any]:
        """Remove one fingerprint. See :func:`drop_record`."""
        await self.async_load()
        return await self._async_commit(drop_record(self._data, apid))

    async def async_replace_all(self, data: dict[str, Any]) -> dict[str, Any]:
        """Overwrite the whole database — the restore path."""
        return await self._async_commit(normalise({**data, "changed": _now()}))

    async def async_clean(self) -> int:
        """Delete everything. Returns how many records were removed.

        The scanners are untouched: every fingerprint that opens a door today keeps
        working. What is lost is the ability to repair a scanner, add one, or
        recover from a factory reset.
        """
        await self.async_load()
        removed = len(self._data.get("records") or {})
        await self._async_commit(empty_vault())
        _LOGGER.warning("Fingerprint database cleared: %d record(s) removed", removed)
        return removed


def async_get_vault(hass: HomeAssistant) -> EkeyVault:
    """The one vault instance for this HA run."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    vault = domain_data.get("_vault_instance")
    if vault is None:
        vault = EkeyVault(hass)
        domain_data["_vault_instance"] = vault
    return vault
