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
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from homeassistant.core import HomeAssistant, callback

from . import vault as vault_mod
from .api import (
    EkeyApiError,
    EkeyAppClient,
    EkeyBusyError,
    EkeyNotFoundError,
    EkeyTemplateRejected,
)
from .const import APP_HTTP_BODY_MAX, DOMAIN, EVENT_VAULT_JOB
# One-way on purpose: enroll.py reaches back for async_capture_enrolled through a
# deferred import inside the function that needs it, so this stays acyclic.
from .enroll import EVENT_ENROLL_PROGRESS
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
# A delete that the scanner acknowledged and did not carry out. Distinct from a
# refusal on purpose: the finger still opens that door, so the database record must
# survive and the report has to name the scanner.
REASON_STILL_PRESENT = "still_present"
REASON_ENROLL_FAILED = "enroll_failed"

# Backstop for an enrollment that never reaches a terminal state. The manager runs
# its own idle watchdog and normally ends the session itself; this only bounds the
# job when even that does not fire, so it is deliberately generous — a person has to
# walk to the door and present a finger several times.
_ENROLL_CEILING_S = 300.0

# Head-room left under the backend's whole-document limit for a users.json PUT.
# The cap applies to the entire document, so the check has to happen before the
# request rather than be discovered as a rejected write.
_USERS_DOC_MARGIN = 512

MAX_FINGER = 10


class JobBusy(RuntimeError):
    """Another job is already running. Raised instead of queueing silently."""


class UnknownScannerJob(KeyError):
    """A job named a scanner that is not loaded.

    Its own type rather than a bare KeyError so the websocket layer can word it for
    the operator, and so an unrelated dictionary slip inside a job is never reported
    as "that scanner is not set up".
    """


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

        ``last_update_success`` is checked as well, because a coordinator refresh
        that fails keeps the previous ``data`` — so a scanner that has gone quiet
        would otherwise keep answering with the list it held minutes ago, and a
        push would write against it.
        """
        if not getattr(self.app_coordinator, "last_update_success", True):
            return False
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


async def async_refresh_scanners(
    hass: HomeAssistant, entry_ids: list[str] | None = None
) -> None:
    """Ask the scanners themselves, now, instead of reading the poll's leftovers.

    ``UPDATE_INTERVAL`` is five minutes. That is the right cadence for entities and
    the wrong one for the two places that compare scanners *against each other*: the
    storage matrix, and the push that decides what to write from exactly that
    comparison. Reading a five-minute-old list makes a door look healthy when its
    template has since been deleted, and — worse — makes a push skip that door,
    because it believes the fingerprint is already there.

    Refreshes run together; the backends serialise their own dispatches anyway. A
    failure is left where it belongs: the coordinator keeps its previous data and
    clears ``last_update_success``, which every reader turns into *unknown* rather
    than into a stale "ok".
    """
    coordinators = [
        ref.app_coordinator
        for ref in scanner_refs(hass, entry_ids)
        if ref.app_coordinator is not None
    ]
    if not coordinators:
        return
    await asyncio.gather(
        *(coordinator.async_refresh_now() for coordinator in coordinators),
        return_exceptions=True,
    )


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
        # Between "someone asked for a job" and "the job exists" there is real work
        # — see _reserve. Without this flag that gap is an open door.
        self._starting = False

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

    def _reserve(self) -> None:
        """Claim the one job slot *before* the slow part, and hold it.

        Every start does real work before it can name a job: loading the vault, and
        asking each scanner what it currently holds — several RS-485 round trips.
        Each await hands control back to the event loop, and until ``_begin`` runs
        there is no job for ``running`` to see, so two clicks seconds apart both got
        through and two fan-outs interleaved writes to one sensor.

        Paired with ``_release`` in a ``finally``: a start that fails must not leave
        the slot held.
        """
        if self.running or self._starting:
            raise JobBusy(
                "another fingerprint job is already running — wait for it to finish "
                "or stop it first"
            )
        self._starting = True

    def _release(self) -> None:
        self._starting = False

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
        self._reserve()
        try:
            # Same reason as the push: without this the list being copied is
            # whatever the five-minute poll last saw, so a fingerprint enrolled on
            # the device's own page a minute ago would be invisible here.
            await async_refresh_scanners(self.hass, [entry_id])

            refs = scanner_refs(self.hass, [entry_id])
            if not refs:
                raise JobBusy(f"scanner {entry_id} is not loaded")
            ref = refs[0]

            wanted = [str(a).strip().lower() for a in apids] if apids else None
            if wanted is None:
                if not ref.list_known:
                    raise ValueError(
                        f"{ref.title} could not be asked which fingerprints it holds, "
                        "so there is nothing to copy yet"
                    )
                wanted = sorted(ref.on_scanner)

            job = self._begin(
                "sync_from_scanner",
                f"Copying from “{ref.title}” into the database",
                len(wanted),
            )
        finally:
            self._release()
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
        self._reserve()
        try:
            # Ask the scanners what they hold *now*. What follows decides what to
            # write to a door controller purely from this comparison, and the poll
            # behind it can be five minutes old: a fingerprint deleted from a
            # scanner since the last poll reads as already present and is never
            # re-written — precisely the "the push only went to one scanner" report
            # this fixes.
            await async_refresh_scanners(self.hass, entry_ids)

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
                    # Unknown is not missing. Pushing here would be a minutes-long
                    # job against a guess.
                    continue
                for apid, record in records.items():
                    if apid not in ref.on_scanner:
                        work.append((apid, record, ref))

            job = self._begin(
                "push",
                f"Copying {len(records)} fingerprint(s) from the database to the scanners",
                len(work),
            )
        finally:
            self._release()
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
            await self._push_one(job, apid, record, ref)

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

    async def _push_one(
        self, job: VaultJob, apid: str, record: dict[str, Any], ref: ScannerRef
    ) -> None:
        """Write one stored template, and its assignment, to one scanner.

        Shared by the push and by the fan-out half of an enrollment, so that a
        fingerprint copied automatically after an enrollment lands under exactly the
        same checks, the same verification and the same reporting as one copied by
        hand. A second implementation of this is a second set of rules about when a
        door is considered to have a fingerprint.
        """
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
            return

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
            return
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
            return
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
            return

        self._record(job, JobItem(
            apid=apid, label=label, state=STATE_OK, scanner=ref.title,
            entry_id=ref.entry_id, detail="stored and verified",
        ))

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

    async def _async_unassign(self, ref: ScannerRef, apid: str) -> bool:
        """Take one APID out of a scanner's user list. Returns whether it changed.

        The mirror of :meth:`_async_assign`, and the second half of a delete: a
        template removed from the sensor while its finger entry stays in the
        document leaves a user apparently holding a fingerprint that no longer
        exists — the reverse of the unassigned-template problem, and just as
        confusing to whoever reads the list next.
        """
        users = await ref.client.async_get_users()
        before = len(users)
        changed = False
        for user in users:
            fingers = [f for f in (user.get("fingers") or []) if isinstance(f, dict)]
            kept = [f for f in fingers if str(f.get("apid", "")).lower() != apid]
            if len(kept) != len(fingers):
                user["fingers"] = kept
                changed = True
        if not changed:
            return False

        # Same two guards as the assignment path: the backend replaces the whole
        # document, and a write must never shorten the user list. Removing a finger
        # never removes a user, so this is a real invariant check, not a formality.
        size = _users_doc_size(users)
        if size > APP_HTTP_BODY_MAX - _USERS_DOC_MARGIN:
            raise ValueError(
                f"“{ref.title}” user list would be {size} bytes, over the "
                f"{APP_HTTP_BODY_MAX}-byte limit its backend accepts"
            )
        if len(users) < before:  # pragma: no cover — defensive
            raise ValueError("refusing to write a shorter user list than was read")

        await ref.client.async_put_users(users)
        if ref.app_coordinator is not None:
            await ref.app_coordinator.async_refresh_now()
        return True

    # ---------------------------------------------------------------- enroll

    async def async_enroll(
        self, entry_id: str, user_id: str, finger: int
    ) -> dict[str, Any]:
        """Enrol on one scanner, take the database's copy, then copy it to the rest.

        This is the one operation that creates a fingerprint rather than moving one,
        and it is the reason the database can be a master copy at all: the template
        is captured while it is new, and every other door is given the *same* APID
        instead of the person presenting the same finger at each of them and ending
        up with one identity per door.
        """
        self._reserve()
        try:
            await async_refresh_scanners(self.hass)

            refs = scanner_refs(self.hass, [entry_id])
            if not refs:
                raise UnknownScannerJob(f"scanner {entry_id} is not loaded")
            ref = refs[0]

            bucket = (self.hass.data.get(DOMAIN) or {}).get(entry_id) or {}
            manager = bucket.get("enroll_manager")
            if manager is None:
                raise UnknownScannerJob(
                    f"“{ref.title}” cannot enrol — it does not serve the app layer"
                )

            others = [r for r in scanner_refs(self.hass) if r.entry_id != entry_id]
            job = self._begin(
                "enroll",
                f"Enrolling on “{ref.title}” and copying to {len(others)} other "
                f"scanner(s)",
                1 + len(others),
            )
        finally:
            self._release()
        self._spawn(
            job, lambda j: self._run_enroll(j, ref, manager, user_id, finger, others)
        )
        return job.as_dict()

    async def _run_enroll(
        self,
        job: VaultJob,
        ref: ScannerRef,
        manager: Any,
        user_id: str,
        finger: int,
        others: list[ScannerRef],
    ) -> None:
        job.phase = "capturing"
        job.message = f"Starting the enrollment on “{ref.title}”…"
        self._emit(job)

        done = asyncio.Event()
        outcome: dict[str, Any] = {}

        # @callback, and not optional. Home Assistant decides where to run a listener
        # by inspecting the function it is handed: without this marking it dispatches
        # it to a worker thread, where hass.bus.async_fire (inside _emit) and
        # asyncio.Event.set are both illegal. _emit then raised before done.set() ran,
        # so the job never learned the enrollment had finished and sat until the
        # 300-second ceiling — a successful enrollment reported as a timeout, with
        # nothing captured and nothing copied.
        @callback
        def _on_progress(event) -> None:
            data = getattr(event, "data", None) or {}
            if data.get("entry_id") != ref.entry_id:
                return
            # The terminal state is recorded FIRST. Waking the job is the one thing
            # that must not depend on anything else here succeeding.
            if data.get("done"):
                outcome.update(data)
                done.set()
            # Relayed so the job dialog shows the same words the enrollment card
            # does — "place the finger", "lift and place again" — instead of a
            # progress bar that sits still for half a minute. Best effort: a failure
            # to describe progress must never strand the job that is making it.
            if data.get("message"):
                job.message = data["message"]
                try:
                    self._emit(job)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Could not publish enrollment progress")

        # Listening BEFORE the start, not after: the first progress event is fired
        # from inside async_start, and a listener attached afterwards can miss a
        # session that failed immediately.
        unsub = self.hass.bus.async_listen(EVENT_ENROLL_PROGRESS, _on_progress)
        apid: str | None = None
        try:
            try:
                apid = await manager.async_start(user_id, finger)
            except Exception as err:  # noqa: BLE001 — reported as the item
                self._record(job, JobItem(
                    apid="", label=f"finger {finger}", state=STATE_FAILED,
                    reason=REASON_ENROLL_FAILED, scanner=ref.title,
                    entry_id=ref.entry_id, detail=str(err),
                ))
                self._finish(job, f"The enrollment could not be started: {err}")
                return

            waited = 0.0
            while not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    waited += 1.0
                    if job.cancelling:
                        job.cancelled = True
                        # The sensor is holding a finger LED on and waiting. Telling
                        # it to stop is the whole point of the button.
                        with contextlib.suppress(Exception):
                            await manager.async_cancel(apid)
                        break
                    if waited >= _ENROLL_CEILING_S:
                        break
        finally:
            unsub()

        if job.cancelled:
            self._record(job, JobItem(
                apid=apid or "", label=f"finger {finger}", state=STATE_SKIPPED,
                reason=REASON_CANCELLED, scanner=ref.title, entry_id=ref.entry_id,
                detail="stopped before the finger was enrolled",
            ))
            self._finish(job, "Stopped. Nothing was copied to the other scanners.")
            return

        if not done.is_set():
            self._record(job, JobItem(
                apid=apid or "", label=f"finger {finger}", state=STATE_FAILED,
                reason=REASON_TIMEOUT, scanner=ref.title, entry_id=ref.entry_id,
                detail=f"the scanner said nothing for {_ENROLL_CEILING_S:.0f} seconds",
            ))
            self._finish(job, "The enrollment did not finish. Nothing was copied.")
            return

        label = f"{outcome.get('username') or 'unknown'} · finger {finger}"
        if not outcome.get("ok"):
            self._record(job, JobItem(
                apid=apid or "", label=label, state=STATE_FAILED,
                reason=REASON_ENROLL_FAILED, scanner=ref.title,
                entry_id=ref.entry_id,
                detail=outcome.get("message") or "the enrollment did not succeed",
            ))
            self._finish(job, "The enrollment failed, so nothing was copied.")
            return

        # The database's copy is normally already taken — enroll.py captures it as
        # part of finishing, before this event is fired, so that it exists before
        # anything else can act on the new fingerprint. This is the fallback for
        # when that read failed: without a stored template there is nothing to copy
        # anywhere, and the job must say so rather than report a clean run.
        await self.vault.async_load()
        record = self.vault.data["records"].get(apid)
        if record is None or not record.get("template"):
            try:
                await async_capture_enrolled(
                    self.hass, ref.entry_id, apid=apid,
                    username=outcome.get("username"), finger=finger,
                )
                await self.vault.async_load()
                record = self.vault.data["records"].get(apid)
            except Exception as err:  # noqa: BLE001 — classified below
                state, reason = _classify(err)
                self._record(job, JobItem(
                    apid=apid or "", label=label, state=STATE_FAILED, reason=reason,
                    scanner=ref.title, entry_id=ref.entry_id,
                    detail=(
                        "the finger is enrolled and works on this scanner, but its "
                        f"template could not be copied into the database: {err}"
                    ),
                ))
                self._finish(
                    job,
                    "Enrolled on this scanner, but the database has no copy — so "
                    "nothing could be copied to the others. Use Sync from a scanner.",
                )
                return

        self._record(job, JobItem(
            apid=apid or "", label=label, state=STATE_OK, scanner=ref.title,
            entry_id=ref.entry_id, detail="enrolled and copied into the database",
        ))

        job.phase = "running"
        for other in others:
            if job.cancelling:
                job.cancelled = True
                break
            await self._push_one(job, apid, record, other)

        counts = job.counts
        if job.cancelled:
            self._finish(
                job,
                f"Enrolled, and copied to {counts['ok'] - 1} of {len(others)} other "
                "scanner(s) before stopping.",
            )
        elif counts["failed"] or counts["skipped"]:
            self._finish(
                job,
                f"Enrolled. {counts['skipped']} skipped and {counts['failed']} failed "
                "on the other scanners — the database has the template, so Push can "
                "finish the job later.",
            )
        else:
            self._finish(
                job,
                f"Enrolled and copied to all {len(others)} other scanner(s).",
            )

    # ----------------------------------------------------------------- purge

    async def async_purge_fingerprint(self, apid: str) -> dict[str, Any]:
        """Delete one fingerprint from every scanner, and only then from the database.

        The order is the whole point, and it is the opposite of what feels natural.
        A record removed first would leave a fingerprint that still opens a door with
        nothing in Home Assistant naming it — this project has already had that bug,
        and it is why every scanner has to *confirm* absence by being re-read before
        the record goes.
        """
        self._reserve()
        try:
            await async_refresh_scanners(self.hass)
            await self.vault.async_load()

            apid = str(apid).strip().lower()
            record = self.vault.data["records"].get(apid)
            label = (
                f"{record.get('username') or 'unknown'} · finger {record.get('finger')}"
                if record
                else apid[:8]
            )
            refs = scanner_refs(self.hass)
            job = self._begin(
                "purge_fingerprint",
                f"Deleting {label} from {len(refs)} scanner(s) and the database",
                len(refs) + 1,          # +1: the database record itself
            )
        finally:
            self._release()
        self._spawn(job, lambda j: self._run_purge(j, apid, label, refs))
        return job.as_dict()

    async def _run_purge(
        self, job: VaultJob, apid: str, label: str, refs: list[ScannerRef]
    ) -> None:
        job.phase = "running"
        confirmed_gone = True

        for ref in refs:
            if job.cancelling:
                job.cancelled = True
                confirmed_gone = False
                break

            job.message = f"Deleting {label} from “{ref.title}”…"
            self._emit(job)

            # An unreadable list cannot confirm anything. Deleting into the dark and
            # calling it done is exactly how a record disappears while the finger
            # still opens that door.
            if not ref.list_known:
                confirmed_gone = False
                self._record(job, JobItem(
                    apid=apid, label=label, state=STATE_FAILED,
                    reason=REASON_LIST_UNKNOWN, scanner=ref.title,
                    entry_id=ref.entry_id,
                    detail=(
                        f"“{ref.title}” could not be asked what it holds, so this "
                        "delete cannot be confirmed there"
                    ),
                ))
                continue

            try:
                if apid in ref.on_scanner:
                    await ref.client.async_delete_fingerprint(apid)
                    # Re-read rather than trust the reply: the delete answering 200
                    # is not the sensor having forgotten the finger.
                    remaining = await ref.client.async_list_fingerprints()
                    if apid in {str(a).lower() for a in remaining}:
                        confirmed_gone = False
                        self._record(job, JobItem(
                            apid=apid, label=label, state=STATE_FAILED,
                            reason=REASON_STILL_PRESENT, scanner=ref.title,
                            entry_id=ref.entry_id,
                            detail=(
                                f"“{ref.title}” still lists this fingerprint after the "
                                "delete — it still opens that door"
                            ),
                        ))
                        continue
                    detail = "deleted and confirmed gone"
                else:
                    detail = "was not on this scanner"

                # Whether or not the sensor held it, the user document may still name
                # it. Both halves have to go, or the list shows a finger nothing backs.
                if await self._async_unassign(ref, apid):
                    detail += ", and removed from its user list"
            except Exception as err:  # noqa: BLE001 — classified below
                confirmed_gone = False
                state, reason = _classify(err)
                self._record(job, JobItem(
                    apid=apid, label=label, state=STATE_FAILED, reason=reason,
                    scanner=ref.title, entry_id=ref.entry_id, detail=str(err),
                ))
                continue

            if ref.app_coordinator is not None:
                with contextlib.suppress(Exception):
                    await ref.app_coordinator.async_refresh_now()

            self._record(job, JobItem(
                apid=apid, label=label, state=STATE_OK, scanner=ref.title,
                entry_id=ref.entry_id, detail=detail,
            ))

        # The database record goes last, and only when every scanner said it is gone.
        if confirmed_gone:
            await self.vault.async_drop(apid)
            self._record(job, JobItem(
                apid=apid, label=label, state=STATE_OK, scanner=None,
                detail="removed from the database",
            ))
            self._finish(job, f"{label} is gone from every scanner and the database.")
            return

        self._record(job, JobItem(
            apid=apid, label=label, state=STATE_SKIPPED, scanner=None,
            reason=REASON_STILL_PRESENT,
            detail=(
                "kept — a copy of this fingerprint is still on a scanner, or a "
                "scanner could not confirm it is gone"
            ),
        ))
        self._finish(
            job,
            "Not deleted everywhere, so the database record was kept. The chips show "
            "which scanners still hold it; running this again is safe.",
        )


async def async_capture_enrolled(
    hass: HomeAssistant,
    entry_id: str,
    *,
    apid: str,
    username: str | None,
    finger: int,
    ha_person: str | None = None,
) -> None:
    """Read a just-enrolled template off its scanner and file it in the database.

    Called from the enrollment itself rather than from a job, so that the copy is
    taken while the session is still the only thing that knows this APID exists —
    before the panel reloads, before any automation reacts, and before anyone can
    delete the finger they have just enrolled. Raises; the caller decides whether a
    missing copy is worth failing over (it is not: the fingerprint works either way
    and shows up as *extra*, one click from being adopted).
    """
    refs = scanner_refs(hass, [entry_id])
    if not refs:
        raise UnknownScannerJob(f"scanner {entry_id} is not loaded")
    ref = refs[0]

    info = await ref.client.async_get_template(apid)
    vault = vault_mod.async_get_vault(hass)
    await vault.async_load()
    await vault.async_put(
        apid=apid,
        username=username,
        finger=finger,
        ha_person=ha_person,
        template=info,
        dev_variant=ref.dev_variant,
        dev_sub_variant=ref.device.get("dev_sub_variant"),
        source_entry_id=ref.entry_id,
        source_scanner_id=ref.scanner_id,
        source_prod_sn=ref.prod_sn,
    )


def async_get_jobs(hass: HomeAssistant) -> VaultJobManager:
    """The one job manager for this HA run."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    manager = domain_data.get(_JOBS_CACHE_KEY)
    if manager is None:
        manager = VaultJobManager(hass)
        domain_data[_JOBS_CACHE_KEY] = manager
    return manager
