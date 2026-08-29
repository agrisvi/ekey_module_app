"""Long-running fingerprint transfers, and how they report themselves.

Copying a template is not a request — it is a job. Every dispatch to a scanner
queues behind one lock in the backend library, a write costs several ~700 ms poll
passes plus a ~1.9 s device-side "template sync", and the hard dispatch ceiling is
15 s. So a fingerprint takes **seconds**, thirty of them take minutes, and the work
has to be strictly sequential. A blocking websocket call would time out long before
the interesting part; what the panel gets instead is a job id and a stream of
progress events, exactly as enrolment already works.

Three rules this module exists to enforce:

* **One job at a time.** Two fan-outs interleaving writes to one sensor is not
  something to find out about afterwards.
* **Cancel happens between items, never inside one.** Interrupting a transfer
  mid-frame is precisely what leaves a half-written template on a sensor.
* **``verified`` decides, never the HTTP status.** A write that was accepted and
  discarded answers 200 with ``rpc_error_code: "OK"``. An item is ``ok`` if and
  only if the scanner confirmed it kept the template — see
  :meth:`~.api.EkeyAppClient.async_put_template`.

Nothing here resumes after a Home Assistant restart, deliberately. Each item is
atomic — one template write, one user-document write, one store save — so a job
that dies leaves no half-finished fingerprint, and the presence matrix shows the
truth on the next read. A resume mechanism would add state that can itself be
wrong about a door.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from homeassistant.core import HomeAssistant

from . import vault as vault_mod
from .api import (
    EkeyApiError,
    EkeyAppClient,
    EkeyBusyError,
    EkeyNotFoundError,
    EkeyTemplateRejected,
)
from .const import APP_HTTP_BODY_MAX, DOMAIN, EVENT_VAULT_JOB
from .templates import DEFAULT_DOMAIN_ID, TemplateError

_LOGGER = logging.getLogger(__name__)

_JOBS_CACHE_KEY = "_vault_jobs"

# Item outcomes. Three, not two: a *skip* is permanent and must never be offered a
# retry (a device variant cannot be changed by anyone but ekey), while a *failure*
# is worth trying again (a full sensor, a scanner that was briefly unreachable).
STATE_OK = "ok"
STATE_SKIPPED = "skipped"
STATE_FAILED = "failed"

# A closed set, so the panel can word each one properly instead of printing
# whatever an exception happened to say.
REASON_VARIANT_MISMATCH = "variant_mismatch"
REASON_NO_TEMPLATE_API = "no_template_api"
REASON_SENSOR_FULL = "sensor_full"
REASON_NOT_VERIFIED = "not_verified"
REASON_UNREACHABLE = "unreachable"
REASON_REFUSED = "refused"
REASON_NO_TEMPLATE = "no_template"
REASON_TIMEOUT = "timeout"
REASON_CANCELLED = "cancelled"
REASON_LIST_UNKNOWN = "list_unknown"
REASON_TEMPLATE_ONLY = "template_only"
REASON_USERS_DOC_TOO_LARGE = "users_doc_too_large"

# Head-room left under the backend's whole-document limit for a users.json PUT.
# The cap applies to the entire document, so the check has to happen before the
# request rather than be discovered as a rejected write.
_USERS_DOC_MARGIN = 512

MAX_FINGER = 10


class JobBusy(RuntimeError):
    """Another job is already running. Raised instead of queueing silently."""


@dataclass
class JobItem:
    """One unit of work and how it turned out."""

    apid: str
    label: str
    state: str
    scanner: str | None = None
    entry_id: str | None = None
    reason: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "apid": self.apid,
            "label": self.label,
            "scanner": self.scanner,
            "entry_id": self.entry_id,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class VaultJob:
    """State of one running or finished job."""

    kind: str
    title: str
    total: int
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: str = "starting"
    message: str = ""
    items: list[JobItem] = field(default_factory=list)
    done: bool = False
    ok: bool | None = None
    cancelled: bool = False
    cancelling: bool = False
    started_at: float = field(default_factory=time.time)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "ok": sum(1 for i in self.items if i.state == STATE_OK),
            "skipped": sum(1 for i in self.items if i.state == STATE_SKIPPED),
            "failed": sum(1 for i in self.items if i.state == STATE_FAILED),
        }

    def as_dict(self, *, item: JobItem | None = None) -> dict[str, Any]:
        """The event payload.

        ``entry_id`` is always ``None`` — and that is not laziness. The panel's
        subscription filter lets a ``None`` through to a scanner-scoped listener, so
        a job stays visible whichever view is open; the scanner being worked on is
        named in ``title`` and in ``item.scanner`` instead.

        ``index``/``total``/``counts`` are absolute rather than incremental, so a
        dropped event costs nothing, and the terminal event carries the full
        ``items`` list so the final report is right even if events were missed.
        """
        return {
            "entry_id": None,
            "job_id": self.job_id,
            "kind": self.kind,
            "title": self.title,
            "phase": self.phase,
            "index": len(self.items),
            "total": self.total,
            "counts": self.counts,
            "message": self.message,
            "item": item.as_dict() if item else None,
            "done": self.done,
            "ok": self.ok,
            "cancelled": self.cancelled,
            "cancelling": self.cancelling,
            "items": [i.as_dict() for i in self.items] if self.done else None,
        }


@dataclass
class ScannerRef:
    """What a job needs to know about one scanner before writing to it."""

    entry_id: str
    title: str
    client: EkeyAppClient
    coordinator: Any
    app_coordinator: Any

    @property
    def device(self) -> dict[str, Any]:
        data = getattr(self.coordinator, "data", None) or {}
        device = data.get("device")
        return device if isinstance(device, dict) else {}

    @property
    def dev_variant(self) -> int | None:
        """The one field that decides whether a template can be copied here at all."""
        value = self.device.get("dev_variant")
        return value if isinstance(value, int) else None

    @property
    def prod_sn(self) -> str | None:
        value = self.device.get("prod_sn")
        return value if isinstance(value, str) else None

    @property
    def scanner_id(self) -> str:
        return self.client.conn.scanner_id

    @property
    def list_known(self) -> bool:
        """Whether the sensor's fingerprint list could be read.

        Never inferred. An unreadable list means *unknown*, and a job must not turn
        that into "missing" and start writing.
        """
        data = getattr(self.app_coordinator, "data", None) or {}
        return bool(data.get("scanner_list_known"))

    @property
    def on_scanner(self) -> set[str]:
        data = getattr(self.app_coordinator, "data", None) or {}
        aps = data.get("scanner_aps")
        return set(aps) if isinstance(aps, list) else set()


def scanner_refs(hass: HomeAssistant, entry_ids: list[str] | None = None) -> list[ScannerRef]:
    """Every loaded scanner, or the named ones. Unloaded entries are skipped."""
    refs: list[ScannerRef] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry_ids is not None and entry.entry_id not in entry_ids:
            continue
        bucket = (hass.data.get(DOMAIN) or {}).get(entry.entry_id)
        if not isinstance(bucket, dict) or "app_client" not in bucket:
            continue
        refs.append(
            ScannerRef(
                entry_id=entry.entry_id,
                title=entry.title,
                client=bucket["app_client"],
                coordinator=bucket.get("coordinator"),
                app_coordinator=bucket.get("app_coordinator"),
            )
        )
    return refs


def _classify(err: Exception) -> tuple[str, str]:
    """Map a backend failure onto (state, reason).

    The distinction that matters is permanent versus retryable: a variant mismatch
    can never succeed and must not be offered a retry, while a full sensor or an
    unreachable scanner is worth another go once the operator has acted.
    """
    text = str(err)
    if isinstance(err, EkeyTemplateRejected):
        # Accepted and discarded — but the verdict says whether that is final.
        # ``device_response`` is the scanner having looked at the template and
        # refused it, which is permanent (the variant or the salt) and must not be
        # offered a retry. ``transport_ack_only`` means nobody ever confirmed
        # either way, which is worth trying again.
        if err.verdict == "transport_ack_only":
            return STATE_FAILED, REASON_NOT_VERIFIED
        return STATE_SKIPPED, REASON_NOT_VERIFIED
    if isinstance(err, EkeyNotFoundError):
        return STATE_SKIPPED, REASON_NO_TEMPLATE_API
    if isinstance(err, EkeyBusyError):
        return STATE_FAILED, REASON_TIMEOUT
    if isinstance(err, TemplateError):
        return STATE_FAILED, REASON_NO_TEMPLATE
    if "Maximum_feature_count_reached" in text:
        return STATE_FAILED, REASON_SENSOR_FULL
    if "Error_encryption" in text or "Security_violation" in text:
        return STATE_SKIPPED, REASON_VARIANT_MISMATCH
    if isinstance(err, EkeyApiError):
        return STATE_FAILED, REASON_REFUSED
    return STATE_FAILED, REASON_REFUSED


TEMPLATE_API_KEY = "template_api"


def remember_template_api(hass: HomeAssistant, entry_id: str, available: bool) -> None:
    """Record whether a scanner turned out to have the template routes.

    Learned from use rather than probed. There is no capability flag for these
    routes and a probe would need a real APID and a scanner round trip *per entry,
    on every page load* — for an answer that only matters once somebody actually
    tries. So: unknown until proven, and never guessed. An older backend answers
    404/501 once, which is remembered, and the storage view can then say why that
    scanner is out of the picture instead of offering pushes that cannot work.
    """
    bucket = (hass.data.get(DOMAIN) or {}).get(entry_id)
    if isinstance(bucket, dict):
        bucket[TEMPLATE_API_KEY] = available


def _users_doc_size(users: list[dict[str, Any]]) -> int:
    return len(json.dumps(users, separators=(",", ":")).encode("utf-8"))


def _find_user(users: list[dict[str, Any]], person_key: str, username: str | None):
    """The user on this scanner that corresponds to a database person.

    A linked Home Assistant person is matched on the link, because that survives a
    rename on any individual scanner. Without one there is nothing to match on but
    the name — the weakness the database's own key inherits, recorded in
    :func:`~.vault.person_key`.
    """
    if vault_mod.is_person_link(person_key):
        for user in users:
            if user.get("ha_person") == person_key:
                return user
    wanted = (username or person_key.removeprefix(vault_mod.NAME_KEY_PREFIX)).strip().casefold()
    for user in users:
        if str(user.get("username", "")).strip().casefold() == wanted:
            return user
    return None


class VaultJobManager:
    """Owns the one job that may be running, and the last one that finished."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.vault = vault_mod.async_get_vault(hass)
        self._job: VaultJob | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return self._job is not None and not self._job.done

    def status(self) -> dict[str, Any] | None:
        """The live job, or the last finished one, for a panel that just loaded."""
        return self._job.as_dict() if self._job else None

    def _emit(self, job: VaultJob, item: JobItem | None = None) -> None:
        self.hass.bus.async_fire(EVENT_VAULT_JOB, job.as_dict(item=item))

    def _record(self, job: VaultJob, item: JobItem) -> None:
        job.items.append(item)
        self._emit(job, item)

    def _begin(self, kind: str, title: str, total: int) -> VaultJob:
        if self.running:
            raise JobBusy(
                "another fingerprint job is already running — wait for it to finish "
                "or stop it first"
            )
        job = VaultJob(kind=kind, title=title, total=total)
        self._job = job
        self._emit(job)
        return job

    def _finish(self, job: VaultJob, message: str) -> None:
        counts = job.counts
        job.done = True
        job.phase = "done"
        job.ok = counts["failed"] == 0 and counts["skipped"] == 0 and not job.cancelled
        job.message = message
        self._emit(job)
        _LOGGER.info(
            "Fingerprint job %s (%s) finished: %s", job.job_id[:8], job.kind, counts
        )

    def async_cancel(self, job_id: str | None = None) -> bool:
        """Ask the running job to stop after the item it is on.

        Not a task cancellation: a template transfer that is already in flight has
        to be allowed to land, because a partially written one is the worst outcome
        available.
        """
        job = self._job
        if job is None or job.done:
            return False
        if job_id and job_id != job.job_id:
            return False
        job.cancelling = True
        job.message = (
            "Stopping after the current fingerprint — the scanner is mid-transfer."
        )
        self._emit(job)
        return True

    async def async_shutdown(self) -> None:
        """Stop a job on unload. The store is consistent at every item boundary."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _spawn(self, job: VaultJob, runner: Callable[[VaultJob], Coroutine]) -> None:
        async def guarded() -> None:
            try:
                await runner(job)
            except asyncio.CancelledError:
                job.cancelled = True
                self._finish(job, "Stopped — Home Assistant is shutting down.")
                raise
            except Exception as err:  # noqa: BLE001 — a job must always terminate
                _LOGGER.exception("Fingerprint job %s failed", job.job_id[:8])
                job.message = str(err)
                self._finish(job, f"The job stopped with an error: {err}")

        self._task = self.hass.async_create_background_task(
            guarded(), f"ekey vault job {job.job_id[:8]}"
        )

    # -------------------------------------------------- sync from a scanner

    async def async_sync_from_scanner(
        self, entry_id: str, apids: list[str] | None = None
    ) -> dict[str, Any]:
        """Read templates off one scanner and store them. Writes nothing to it.

        Also the "adopt" path: one APID is the same operation with ``total: 1``, and
        a three-second read that can fail three different ways deserves the same
        reporting as a thirty-item sweep.
        """
        refs = scanner_refs(self.hass, [entry_id])
        if not refs:
            raise JobBusy(f"scanner {entry_id} is not loaded")
        ref = refs[0]

        wanted = [str(a).strip().lower() for a in apids] if apids else None
        if wanted is None:
            if not ref.list_known:
                raise ValueError(
                    f"{ref.title} could not be asked which fingerprints it holds, so "
                    "there is nothing to copy yet"
                )
            wanted = sorted(ref.on_scanner)

        job = self._begin(
            "sync_from_scanner",
            f"Copying from “{ref.title}” into the database",
            len(wanted),
        )
        self._spawn(job, lambda j: self._run_sync(j, ref, wanted))
        return job.as_dict()

    async def _run_sync(self, job: VaultJob, ref: ScannerRef, apids: list[str]) -> None:
        await self.vault.async_load()
        job.phase = "running"

        # One read of the user document, so a thirty-fingerprint sweep does not ask
        # thirty times for a document that cannot change while we hold the lock.
        try:
            users = await ref.client.async_get_users()
        except EkeyApiError as err:
            _LOGGER.debug("Could not read users from %s: %s", ref.title, err)
            users = []

        owner: dict[str, tuple[dict[str, Any], int]] = {}
        for user in users:
            for finger in user.get("fingers") or []:
                if isinstance(finger, dict) and finger.get("apid"):
                    owner[str(finger["apid"]).lower()] = (user, finger.get("finger"))

        for apid in apids:
            if job.cancelling:
                job.cancelled = True
                break

            user, finger = owner.get(apid, (None, None))
            label = (
                f"{user.get('username')} · finger {finger}"
                if user
                else f"unassigned on {ref.title}"
            )
            job.message = f"Copying {len(job.items) + 1} of {job.total} — {label}"
            self._emit(job)

            try:
                info = await ref.client.async_get_template(apid)
            except Exception as err:  # noqa: BLE001 — classified below
                state, reason = _classify(err)
                if reason == REASON_NO_TEMPLATE_API:
                    remember_template_api(self.hass, ref.entry_id, False)
                self._record(job, JobItem(
                    apid=apid, label=label, state=state, reason=reason,
                    scanner=ref.title, entry_id=ref.entry_id, detail=str(err),
                ))
                continue
            remember_template_api(self.hass, ref.entry_id, True)

            await self.vault.async_put(
                apid=apid,
                username=user.get("username") if user else None,
                finger=finger if isinstance(finger, int) else 0,
                ha_person=(user or {}).get("ha_person"),
                template=info,
                dev_variant=ref.dev_variant,
                dev_sub_variant=ref.device.get("dev_sub_variant"),
                source_entry_id=ref.entry_id,
                source_scanner_id=ref.scanner_id,
                source_prod_sn=ref.prod_sn,
            )
            self._record(job, JobItem(
                apid=apid, label=label, state=STATE_OK, scanner=ref.title,
                entry_id=ref.entry_id,
                detail=f"stored, {info.byte_len / 1024:.1f} kB",
            ))

        counts = job.counts
        if job.cancelled:
            self._finish(
                job,
                f"Stopped — {counts['ok']} of {job.total} copied. Those are kept.",
            )
        else:
            self._finish(job, f"Copied {counts['ok']} of {job.total} fingerprint(s).")

    # ------------------------------------------------------------------ push

    async def async_push(
        self, apids: list[str] | None = None, entry_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """Write stored templates to the scanners that do not have them.

        Only ever from an explicit click. Nothing in this integration calls it on a
        timer or on reconnect: pushing a fingerprint grants physical access, and
        that is the one operation worth keeping a person in front of.
        """
        await self.vault.async_load()
        records = vault_mod.pushable(self.vault.data)
        if apids:
            chosen = {str(a).strip().lower() for a in apids}
            records = {a: r for a, r in records.items() if a in chosen}
        records = {a: r for a, r in records.items() if not r.get("superseded_by")}

        refs = scanner_refs(self.hass, entry_ids)
        work: list[tuple[str, dict[str, Any], ScannerRef]] = []
        for ref in refs:
            if not ref.list_known:
                # Unknown is not missing. Pushing here would be a minutes-long job
                # against a guess.
                continue
            for apid, record in records.items():
                if apid not in ref.on_scanner:
                    work.append((apid, record, ref))

        job = self._begin(
            "push",
            f"Copying {len(records)} fingerprint(s) from the database to the scanners",
            len(work),
        )
        self._spawn(job, lambda j: self._run_push(j, work))
        return job.as_dict()

    async def _run_push(
        self, job: VaultJob, work: list[tuple[str, dict[str, Any], ScannerRef]]
    ) -> None:
        job.phase = "running"

        for apid, record, ref in work:
            if job.cancelling:
                job.cancelled = True
                break

            label = f"{record.get('username') or 'unknown'} · finger {record.get('finger')}"
            job.message = (
                f"Writing {len(job.items) + 1} of {job.total} — {label} "
                f"to “{ref.title}”"
            )
            self._emit(job)

            # Checked before the write, not discovered from a rejection: a variant
            # mismatch can never be made to work, and only ekey can change it.
            if (
                record.get("dev_variant") is not None
                and ref.dev_variant is not None
                and record["dev_variant"] != ref.dev_variant
            ):
                self._record(job, JobItem(
                    apid=apid, label=label, state=STATE_SKIPPED,
                    reason=REASON_VARIANT_MISMATCH, scanner=ref.title,
                    entry_id=ref.entry_id,
                    detail=(
                        f"this template came from a variant-{record['dev_variant']} "
                        f"scanner and “{ref.title}” is variant "
                        f"{ref.dev_variant} — a template can never be copied "
                        "between them"
                    ),
                ))
                continue

            try:
                await ref.client.async_put_template(
                    record["template"], domain_id=record.get("domain_id") or DEFAULT_DOMAIN_ID
                )
            except Exception as err:  # noqa: BLE001 — classified below
                state, reason = _classify(err)
                if reason == REASON_NO_TEMPLATE_API:
                    remember_template_api(self.hass, ref.entry_id, False)
                self._record(job, JobItem(
                    apid=apid, label=label, state=state, reason=reason,
                    scanner=ref.title, entry_id=ref.entry_id, detail=str(err),
                ))
                continue
            remember_template_api(self.hass, ref.entry_id, True)

            # The template is on the sensor now. Everything below is the assignment,
            # and its failure is reported as template_only rather than as a failed
            # write, because retrying the write would be pointless and the operator
            # needs to know the finger already opens that door.
            try:
                await self._async_assign(ref, record, apid)
            except ValueError as err:
                self._record(job, JobItem(
                    apid=apid, label=label, state=STATE_FAILED,
                    reason=REASON_USERS_DOC_TOO_LARGE, scanner=ref.title,
                    entry_id=ref.entry_id, detail=str(err),
                ))
                continue
            except EkeyApiError as err:
                self._record(job, JobItem(
                    apid=apid, label=label, state=STATE_FAILED,
                    reason=REASON_TEMPLATE_ONLY, scanner=ref.title,
                    entry_id=ref.entry_id,
                    detail=(
                        "the template is on the scanner and works, but its user list "
                        f"could not be updated, so nobody is named for it: {err}"
                    ),
                ))
                continue

            self._record(job, JobItem(
                apid=apid, label=label, state=STATE_OK, scanner=ref.title,
                entry_id=ref.entry_id, detail="stored and verified",
            ))

        counts = job.counts
        if job.cancelled:
            self._finish(job, f"Stopped — {counts['ok']} of {job.total} written.")
        elif counts["failed"] or counts["skipped"]:
            self._finish(
                job,
                f"{counts['ok']} written, {counts['skipped']} skipped, "
                f"{counts['failed']} failed. The ones that did not go through "
                "changed nothing.",
            )
        else:
            self._finish(job, f"All {counts['ok']} written and verified.")

    async def _async_assign(
        self, ref: ScannerRef, record: dict[str, Any], apid: str
    ) -> None:
        """Give the freshly written template an owner in that scanner's user list.

        Without this the scanner recognises the finger and reports an APID nobody
        claims — the door opens, but only Home Assistant can say who walked through
        it. Writing it means the scanner stays self-sufficient when HA is down,
        which is the property this integration advertises.
        """
        users = await ref.client.async_get_users()
        before = len(users)
        person_key = record.get("person_key") or ""
        username = record.get("username")

        user = _find_user(users, person_key, username)
        if user is None:
            user = {
                "id": str(uuid.uuid4()),
                "username": username or person_key.removeprefix(vault_mod.NAME_KEY_PREFIX),
                "fingers": [],
            }
            if vault_mod.is_person_link(person_key):
                user["ha_person"] = person_key
            users.append(user)

        finger = record.get("finger")
        fingers = [
            f
            for f in (user.get("fingers") or [])
            if isinstance(f, dict)
            and str(f.get("apid", "")).lower() != apid
            and f.get("finger") != finger
        ]
        entry: dict[str, Any] = {"apid": apid, "enrolled_at": int(time.time())}
        if isinstance(finger, int) and 1 <= finger <= MAX_FINGER:
            entry["finger"] = finger
        fingers.append(entry)
        user["fingers"] = sorted(fingers, key=lambda f: f.get("finger") or 0)

        # The backend replaces the WHOLE document on a PUT and caps the body, so a
        # document that has outgrown the limit must be refused here — discovering
        # it as a rejected write would leave the template assigned to nobody.
        size = _users_doc_size(users)
        if size > APP_HTTP_BODY_MAX - _USERS_DOC_MARGIN:
            raise ValueError(
                f"“{ref.title}” user list would be {size} bytes, over the "
                f"{APP_HTTP_BODY_MAX}-byte limit its backend accepts. Remove some "
                "fingerprints there, or split the users across scanners."
            )
        # Never shrink the document: a partial read followed by a full write is how
        # an entire user list disappears.
        if len(users) < before:  # pragma: no cover — defensive
            raise ValueError("refusing to write a shorter user list than was read")

        await ref.client.async_put_users(users)
        if ref.app_coordinator is not None:
            await ref.app_coordinator.async_refresh_now()


def async_get_jobs(hass: HomeAssistant) -> VaultJobManager:
    """The one job manager for this HA run."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    manager = domain_data.get(_JOBS_CACHE_KEY)
    if manager is None:
        manager = VaultJobManager(hass)
        domain_data[_JOBS_CACHE_KEY] = manager
    return manager
