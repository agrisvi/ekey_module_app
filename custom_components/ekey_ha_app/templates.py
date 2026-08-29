"""Fingerprint template blobs: validation, and reading the identity out of one.

A template is the only thing in this system that cannot be re-derived. If a sensor
is replaced, every person on it comes back and presents a finger again — unless
Home Assistant kept a copy. So the copy has to be trustworthy, and the one thing
this module exists to guarantee is that **whatever we store is a template and not
something that merely looked like one**.

That is not a hypothetical. The documented shell recipe for backing up a template
by hand pipes ``curl`` through ``sed``, and a ``sed`` substitution prints its input
UNCHANGED when the pattern does not match — so an error reply lands in the ``.txt``
file and is later fed back to the scanner as if it were a fingerprint. The C suite
carries a regression test for exactly that. This module is the same guard on the
Home Assistant side, and it runs on every boundary: adopting from a scanner,
restoring a backup, pushing to a scanner, and loading the store.

The blob is self-describing, which is what makes the check cheap and strong::

    apFingerTemplate := [len uint16 LE][TIF ...]
    TIF              := [version uint32][AP-ID 16 bytes][type int32]
                        ... [AES-GCM ciphertext][16-byte tag]

The AP-ID is *associated data* for the AEAD, not ciphertext, so it sits there in
plain sight — the scanner reads the identity out of the template it is handed
rather than being told it separately. Two consequences the whole central-database
design rests on:

* a template written to a second scanner keeps its APID, so one physical finger
  has one identity across the whole fleet;
* a stored record can be checked against itself — the declared length must match
  the actual length, and the embedded APID must match the key we filed it under.

Verified against two real dumps from the fleet::

    "791C" "00000000" "630D687FB6174E8EB62B08F36405E6F7" ...
     ^len   ^version   ^AP-ID  -> 630d687f-b617-4e8e-b62b-08f36405e6f7
    0x1C79 = 7289 bytes, and len(hex) == 4 + 2*7289 == 14582.

One thing this module deliberately does NOT do is compare two templates for
equality. The scanner encrypts the body with a fresh random IV on every read, so
two reads of the same finger never match byte for byte; a byte comparison would
report every healthy fingerprint as changed.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

# The backend's MAX_FINGERPRINT_SIZE, applied to the whole blob (length prefix
# included) because that is the form the documentation states it in: 10000 bytes
# is 20000 hex characters. Real templates measure ~7 KB, so the headroom is wide
# either way and this cap is here to reject nonsense, not to police a boundary.
MAX_TEMPLATE_BYTES = 10000

# The salt in the device's transport-key derivation. NOT decoration: a template
# read under one domainID cannot be written back under another, and the failure is
# silent — the scanner accepts the transfer and discards it. The value must
# therefore travel with every stored blob, and this default is what the backend
# substitutes when a request omits the field.
DEFAULT_DOMAIN_ID = "avubs"

# Offsets into the HEX STRING, so each is twice its byte offset: a uint16 length
# prefix, then a uint32 version, then the 16-byte AP-ID.
_LEN_END = 4       # hex[0:4]   bytes 0..1   uint16 LE — length of the TIF
_VERSION_END = 12  # hex[4:12]  bytes 2..5   uint32 LE — TIF version
_APID_END = 44     # hex[12:44] bytes 6..21  the AP-ID

_HEX_DIGITS = frozenset("0123456789ABCDEF")


class TemplateError(ValueError):
    """A blob is not a fingerprint template, or not the one that was asked for.

    A ``ValueError`` subclass on purpose: ``ws_api._handle_errors`` already maps
    ``ValueError`` onto the ``invalid_request`` websocket error, so a rejected
    template reaches the panel as a bad request rather than as a traceback.
    """


@dataclass(frozen=True)
class TemplateInfo:
    """A validated template, and what could be read out of it without a key."""

    apid: str
    """The AP-ID from the blob's own plaintext header, in canonical UUID form."""

    tif_len: int
    """Declared length of the TIF — everything after the 2-byte length prefix."""

    version: int
    """TIF version. 0 on every template seen so far; see :func:`parse_template_hex`."""

    hex: str
    """The blob, stripped and uppercased — the form to store and to send."""

    sha256: str
    """Digest of :attr:`hex`. For change detection, never for comparing fingers."""

    domain_id: str = DEFAULT_DOMAIN_ID
    """The salt this blob was read under, and the only one it can be written under.

    Not part of the blob — it comes from the reply that carried it, and a read that
    omitted the field used the backend's default. Carried here so a stored record
    can never be separated from the one value that makes it restorable.
    """

    @property
    def byte_len(self) -> int:
        """Size of the whole blob, length prefix included."""
        return len(self.hex) // 2


def _u_le(hex_text: str) -> int:
    """Decode a little-endian unsigned integer written as hex characters."""
    return int.from_bytes(bytes.fromhex(hex_text), "little")


def parse_template_hex(text: str, *, expect_apid: str | None = None) -> TemplateInfo:
    """Validate a blob and read its identity out of it.

    Raises :class:`TemplateError` with a sentence naming what was wrong. These
    messages reach the operator, so "not valid" is not good enough: the two
    failures that actually happen — an error reply saved in place of a template,
    and a file cut short by a failed download — each say so in as many words.

    ``expect_apid`` cross-checks the embedded identity against the APID the caller
    believes it asked for. Always pass it when there is one. It is also what makes
    an unrecognised future TIF version safe: if a later layout moved the AP-ID,
    this check fails and the template is refused rather than filed under the wrong
    finger. That is why the version is recorded and not enforced — refusing an
    unknown version outright would break on a firmware update that is otherwise
    perfectly readable.
    """
    if not isinstance(text, str):
        raise TemplateError("a template must be a hex string")

    blob = text.strip().upper()
    if not blob:
        raise TemplateError("the template is empty")

    # Checked ahead of the charset, because this is the failure that really
    # occurs and a bare "not hexadecimal" would send someone hunting for a typo.
    if blob.startswith(("{", "[")):
        raise TemplateError(
            "that is a JSON document, not a template — an error reply was probably "
            "saved in place of the fingerprint"
        )
    if not _HEX_DIGITS.issuperset(blob):
        bad = "".join(sorted({c for c in blob if c not in _HEX_DIGITS})[:4])
        raise TemplateError(f"the template is not hexadecimal (found {bad!r})")
    if len(blob) % 2:
        raise TemplateError("the template has an odd number of hex digits")
    if len(blob) < _APID_END:
        raise TemplateError(
            f"the template is {len(blob) // 2} bytes long — too short to hold a header"
        )

    declared = _u_le(blob[:_LEN_END])
    if len(blob) != _LEN_END + 2 * declared:
        # The check that catches a truncated download, and the reason a
        # half-written backup file can never reach a scanner.
        raise TemplateError(
            f"the template says it holds {declared} bytes but carries "
            f"{(len(blob) - _LEN_END) // 2} — it is incomplete or damaged"
        )
    if len(blob) // 2 > MAX_TEMPLATE_BYTES:
        raise TemplateError(
            f"the template is {len(blob) // 2} bytes, over the {MAX_TEMPLATE_BYTES}-byte "
            "limit the scanner accepts"
        )

    try:
        apid = str(uuid.UUID(hex=blob[_VERSION_END:_APID_END]))
    except ValueError as err:  # pragma: no cover — unreachable once the charset holds
        raise TemplateError(
            "the template header does not carry a readable AP-ID"
        ) from err

    if expect_apid is not None and apid != str(expect_apid).strip().lower():
        raise TemplateError(
            f"the template carries fingerprint {apid}, not {expect_apid} — "
            "the scanner answered about a different finger"
        )

    return TemplateInfo(
        apid=apid,
        tif_len=declared,
        version=_u_le(blob[_LEN_END:_VERSION_END]),
        hex=blob,
        sha256=hashlib.sha256(blob.encode("ascii")).hexdigest(),
    )


def is_template_hex(text: str) -> bool:
    """Whether ``text`` would survive :func:`parse_template_hex`. For previews."""
    try:
        parse_template_hex(text)
    except TemplateError:
        return False
    return True
