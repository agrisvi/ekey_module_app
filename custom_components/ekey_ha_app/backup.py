"""Backup and restore for the fingerprint database.

A backup file is the only way a template ever leaves this Home Assistant, and it is
the copy that actually walks away — onto a laptop, into a mail attachment, onto a
USB stick. Anyone holding it can write those fingerprints into any scanner of the
same device variant, which is to say they can mint working credentials for the
building. That is why the file is encrypted by default and why the unencrypted
option has to be chosen deliberately.

The encryption runs **here, in Python**, not in the browser. Home Assistant is
commonly served over plain HTTP on a LAN address, and ``window.crypto.subtle`` does
not exist outside a secure context — a page that tried would simply have no crypto
to call. ``cryptography`` is already a hard Home Assistant dependency, so this
costs no new requirement.

The envelope keeps its metadata **outside** the ciphertext on purpose::

    { "format": "ekey-fingerprint-backup", "version": 1,
      "created": ..., "record_count": 27, "dev_variants": [10],
      "encryption": { "kdf": "scrypt", ... } | null,
      "payload": "<base64 ciphertext>" }

That is what lets the restore dialog say what a file claims to hold before asking
anyone for a passphrase. The header is not merely decorative, though: it is fed to
AES-GCM as **associated data**, so editing any field in it breaks decryption. A
preview can therefore be shown before the secret is known and still be trustworthy
once it is — with the one caveat the UI must word carefully, that an
as-yet-unverified header is a *claim*.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .templates import DEFAULT_DOMAIN_ID, TemplateError, parse_template_hex

_LOGGER = logging.getLogger(__name__)

BACKUP_FORMAT = "ekey-fingerprint-backup"
BACKUP_VERSION = 1

# scrypt rather than PBKDF2: the threat here is an offline dictionary attack on a
# file somebody copied, and scrypt's memory cost is what makes that expensive on
# the kind of hardware an attacker actually rents. n=2**14 with r=8 needs ~16 MB
# and about a tenth of a second — bearable in an executor, unpleasant at scale.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_BYTES = 32          # AES-256
SALT_BYTES = 16
NONCE_BYTES = 12        # GCM's standard nonce length

MIN_PASSPHRASE = 8


class BackupError(ValueError):
    """A backup file could not be produced, read, or trusted.

    ``ValueError`` so that ``ws_api._handle_errors`` reports it as
    ``invalid_request`` — a bad file is a bad request, not a backend outage.
    """


class BackupLocked(BackupError):
    """The file is encrypted and the passphrase was wrong or absent."""


def _canonical(header: dict[str, Any]) -> bytes:
    """The exact bytes that get authenticated.

    Sorted keys and no whitespace, so the same header always produces the same
    associated data regardless of how the JSON happened to be written.
    """
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _derive(passphrase: str, salt: bytes) -> bytes:
    """Turn a passphrase into a key. CPU-bound — call it in an executor."""
    return Scrypt(
        salt=salt, length=KEY_BYTES, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P
    ).derive(passphrase.encode("utf-8"))


def _payload_of(vault_data: dict[str, Any]) -> dict[str, Any]:
    """The part of the database a backup carries, plus its own digest."""
    body = {
        "records": vault_data.get("records") or {},
        "people": vault_data.get("people") or {},
    }
    body["sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    return body


def _summarise(vault_data: dict[str, Any]) -> dict[str, Any]:
    """What the header says about the contents.

    ``dev_variants`` and ``domain_ids`` are here because they decide whether these
    templates can be used at all: a variant this fleet does not have means every
    record is unpushable, and that is worth saying in a preview rather than
    discovering one failed write at a time.
    """
    records = (vault_data.get("records") or {}).values()
    variants = sorted(
        {r.get("dev_variant") for r in records if r.get("dev_variant") is not None}
    )
    domains = sorted(
        {r.get("domain_id") or DEFAULT_DOMAIN_ID for r in records}
    )
    return {
        "record_count": len(list(records)),
        "user_count": len(vault_data.get("people") or {}),
        "has_templates": any(r.get("template") for r in records),
        "dev_variants": variants,
        "domain_ids": domains,
    }


def create(
    vault_data: dict[str, Any],
    *,
    passphrase: str | None,
    created_by: str,
    installation: str,
    now: datetime | None = None,
) -> tuple[bytes, str]:
    """Build a backup file. Returns ``(bytes, filename)``.

    ``passphrase=None`` writes a readable file with a warning key in it — offered
    because interoperability and inspectability are legitimate, not because it is
    safe. Everything else about the two forms is identical, so a plain file can be
    diffed against an encrypted one's preview.

    CPU-bound when a passphrase is given (see :func:`_derive`); call via
    :func:`async_create`.
    """
    stamp = now or datetime.now(timezone.utc)
    header: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "created": stamp.isoformat(timespec="seconds"),
        "created_by": created_by,
        # A truncated hash, not the instance id itself: enough to recognise "this
        # came from somewhere else", not enough to be an identifier worth having.
        "installation": installation,
        **_summarise(vault_data),
    }
    payload = _payload_of(vault_data)
    date = stamp.strftime("%Y-%m-%d")

    if passphrase is None:
        envelope = {
            **header,
            "encryption": None,
            "WARNING": (
                "This file contains working fingerprint templates in plain text. "
                "Anyone who copies it can write these fingerprints into another "
                "scanner. Store it somewhere private and delete it when done."
            ),
            "payload": payload,
        }
        return (
            json.dumps(envelope, indent=2).encode("utf-8"),
            f"ekey-fingerprints-{date}.json",
        )

    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(
            f"the passphrase must be at least {MIN_PASSPHRASE} characters"
        )

    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    header["encryption"] = {
        "kdf": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": base64.b64encode(salt).decode("ascii"),
        "cipher": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }
    key = _derive(passphrase, salt)
    sealed = AESGCM(key).encrypt(
        nonce, json.dumps(payload, separators=(",", ":")).encode("utf-8"), _canonical(header)
    )
    envelope = {**header, "payload": base64.b64encode(sealed).decode("ascii")}
    return (
        json.dumps(envelope, indent=2).encode("utf-8"),
        f"ekey-fingerprints-{date}.ekeybak",
    )


def read_header(raw: bytes) -> tuple[dict[str, Any], bool]:
    """Parse a file far enough to describe it. Returns ``(header, encrypted)``.

    Deliberately does not need the passphrase: this is what fills the restore
    dialog's "it says it holds 27 fingerprints" line. Nothing here is verified yet,
    which is why the caller's wording has to stay in the conditional.
    """
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as err:
        raise BackupError("that file is not text — it is not an ekey backup") from err
    except ValueError as err:
        raise BackupError(
            "that file is not readable as JSON — it may be truncated or not a backup"
        ) from err

    if not isinstance(envelope, dict):
        raise BackupError("that file is not an ekey backup")
    if envelope.get("format") != BACKUP_FORMAT:
        raise BackupError(
            "that file is not an ekey fingerprint backup (no format marker)"
        )
    version = envelope.get("version")
    if version != BACKUP_VERSION:
        raise BackupError(
            f"that backup is version {version}; this integration reads version "
            f"{BACKUP_VERSION}"
        )

    header = {k: v for k, v in envelope.items() if k not in ("payload", "WARNING")}
    return header, envelope.get("encryption") is not None


def open_payload(raw: bytes, passphrase: str | None = None) -> dict[str, Any]:
    """Return the records a backup carries, verifying it on the way through.

    Raises :class:`BackupLocked` when a passphrase is needed or wrong, and
    :class:`BackupError` when the file is damaged or has been edited. There is no
    third outcome: everything that reaches a caller here has been authenticated
    against the header it was shown with.
    """
    envelope = json.loads(raw.decode("utf-8"))
    header, encrypted = read_header(raw)
    payload = envelope.get("payload")

    if not encrypted:
        if not isinstance(payload, dict):
            raise BackupError("the backup carries no readable records")
        return _verified(payload)

    if not passphrase:
        raise BackupLocked("this backup is encrypted — a passphrase is needed")
    if not isinstance(payload, str):
        raise BackupError("the backup's encrypted payload is missing or malformed")

    settings = header.get("encryption") or {}
    try:
        salt = base64.b64decode(settings["salt"], validate=True)
        nonce = base64.b64decode(settings["nonce"], validate=True)
        sealed = base64.b64decode(payload, validate=True)
    except (KeyError, ValueError, TypeError) as err:
        raise BackupError("the backup's encryption details are damaged") from err

    if settings.get("kdf") != "scrypt" or settings.get("cipher") != "AES-256-GCM":
        raise BackupError(
            f"unsupported encryption in that backup ({settings.get('kdf')} / "
            f"{settings.get('cipher')})"
        )

    # Always through the parameter-honouring path: a file written by a future
    # build with a higher cost must still open, and the bounds in there are what
    # stop a hostile file from asking for 16 GB of scrypt memory.
    key = _derive_custom(passphrase, salt, settings)

    try:
        opened = AESGCM(key).decrypt(nonce, sealed, _canonical(header))
    except InvalidTag as err:
        # One exception, three causes, and the message must not pretend to know
        # which: a wrong passphrase, a truncated payload, and a header somebody
        # edited all land here. The header being associated data is what makes the
        # last one detectable at all.
        raise BackupLocked(
            "that passphrase does not open this file, or the file has been changed "
            "since it was created. Nothing has been restored."
        ) from err

    try:
        body = json.loads(opened.decode("utf-8"))
    except ValueError as err:  # pragma: no cover — authenticated bytes were not JSON
        raise BackupError("the backup decrypted to something that is not JSON") from err
    if not isinstance(body, dict):
        raise BackupError("the backup decrypted to something that is not a record set")
    return _verified(body)


def _derive_custom(passphrase: str, salt: bytes, settings: dict[str, Any]) -> bytes:
    """Honour the KDF parameters a file was written with, within reason.

    A future build may raise the cost; refusing to read its files would be worse
    than paying it. The bounds exist so a hostile file cannot ask for 16 GB of
    scrypt memory and take Home Assistant down as a denial of service.
    """
    n = settings.get("n", SCRYPT_N)
    r = settings.get("r", SCRYPT_R)
    p = settings.get("p", SCRYPT_P)
    if not all(isinstance(v, int) for v in (n, r, p)):
        raise BackupError("the backup's encryption parameters are not numbers")
    if not (2**10 <= n <= 2**20) or not (1 <= r <= 32) or not (1 <= p <= 16):
        raise BackupError("the backup asks for encryption parameters outside safe limits")
    return Scrypt(salt=salt, length=KEY_BYTES, n=n, r=r, p=p).derive(
        passphrase.encode("utf-8")
    )


def _verified(body: dict[str, Any]) -> dict[str, Any]:
    """Check the payload's own digest, then hand back the records."""
    claimed = body.get("sha256")
    records = body.get("records")
    people = body.get("people")
    if not isinstance(records, dict) or not isinstance(people, dict):
        raise BackupError("the backup carries no readable records")

    if isinstance(claimed, str):
        actual = hashlib.sha256(_canonical({"records": records, "people": people})).hexdigest()
        if actual != claimed:
            raise BackupError(
                "the backup's contents do not match its own checksum — it is damaged"
            )
    return {"records": records, "people": people}


def validate_records(records: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Split a restored record set into what is usable and what is not.

    A backup is untrusted input even when it authenticates: it may have been made
    by an older build, or hand-assembled from the shell recipe. Every template goes
    through :func:`~.templates.parse_template_hex` and every record must own the
    APID it is filed under — so a blob that is really an error reply, or a record
    misfiled under another finger's id, is reported by name instead of being
    restored and pushed to a door later.

    Returns ``(good, problems)`` where ``problems`` are sentences for the operator.
    """
    good: dict[str, Any] = {}
    problems: list[str] = []

    for apid, record in records.items():
        if not isinstance(record, dict):
            problems.append(f"{apid}: not a record")
            continue
        template = record.get("template")
        if template is None:
            # Metadata-only records are legitimate — they name a finger that lives
            # on a scanner whose template was never captured.
            good[apid] = record
            continue
        try:
            parse_template_hex(template, expect_apid=apid)
        except TemplateError as err:
            problems.append(f"{apid}: {err}")
            continue
        good[apid] = record

    return good, problems


# --------------------------------------------------------------- async wrappers


async def async_create(hass, vault_data, **kwargs) -> tuple[bytes, str]:
    """:func:`create`, with the key derivation kept off the event loop."""
    return await hass.async_add_executor_job(
        lambda: create(vault_data, **kwargs)
    )


async def async_open_payload(hass, raw: bytes, passphrase: str | None = None):
    """:func:`open_payload`, with the key derivation kept off the event loop."""
    return await hass.async_add_executor_job(lambda: open_payload(raw, passphrase))
