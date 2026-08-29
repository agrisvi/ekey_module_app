"""Chunked file transfer over the websocket, in both directions.

A backup of a hundred fingerprints is about 1.5 MB, and this integration has no
HTTP view — the panel speaks only ``hass.callWS``, deliberately, because that is
the connection that is already authenticated and already has the token nowhere near
the browser. Two limits then decide the shape of this module:

* what the browser **sends** is capped at aiohttp's 4 MiB default, so an upload has
  to arrive in pieces;
* what the server sends is uncapped in principle, but a single multi-megabyte frame
  is a bad neighbour on a busy connection, and a download in pieces is also what
  gives the panel a progress bar for free.

So a transfer is a short-lived buffer with an id, filled or drained one chunk at a
time, strictly in order. Strictly, because there is one buffer per transfer and
parallel chunk requests would race it — the panel drives the loop sequentially and
this side does not try to be clever about that.

Buffers are held in memory and swept on age. They are not persisted: a transfer
interrupted by a restart is one the operator repeats, and holding half an upload
across a reboot would mean writing an unverified biometric file to disk in order to
save a click.
"""
from __future__ import annotations

import base64
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .const import DOMAIN, MAX_RESTORE_BYTES, VAULT_CHUNK_BYTES

_LOGGER = logging.getLogger(__name__)

_TRANSFERS_CACHE_KEY = "_vault_transfers"

# A transfer nobody touched for this long is abandoned — a closed browser tab, a
# dialog cancelled by navigating away. Long enough for a slow upload to pause,
# short enough that a forgotten one does not sit on memory indefinitely.
_IDLE_TIMEOUT = 600.0


class TransferError(ValueError):
    """A transfer id is unknown, expired, or was driven out of order."""


@dataclass
class _Download:
    """A prepared file waiting to be pulled down in pieces."""

    payload: bytes
    filename: str
    transfer_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    touched: float = field(default_factory=time.time)

    @property
    def chunks(self) -> int:
        return max(1, -(-len(self.payload) // VAULT_CHUNK_BYTES))

    def chunk(self, index: int) -> dict[str, Any]:
        if not 0 <= index < self.chunks:
            raise TransferError(
                f"chunk {index} is outside this transfer (it has {self.chunks})"
            )
        self.touched = time.time()
        start = index * VAULT_CHUNK_BYTES
        blob = self.payload[start : start + VAULT_CHUNK_BYTES]
        return {
            "index": index,
            "chunks": self.chunks,
            "last": index == self.chunks - 1,
            "data": base64.b64encode(blob).decode("ascii"),
        }


@dataclass
class _Upload:
    """An incoming file being reassembled."""

    filename: str
    size: int
    chunks: int
    transfer_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    touched: float = field(default_factory=time.time)
    parts: dict[int, bytes] = field(default_factory=dict)

    def add(self, index: int, data: str) -> int:
        if not 0 <= index < self.chunks:
            raise TransferError(
                f"chunk {index} is outside this upload (it expects {self.chunks})"
            )
        try:
            blob = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as err:
            raise TransferError(f"chunk {index} is not valid base64") from err

        self.parts[index] = blob
        self.touched = time.time()
        received = sum(len(p) for p in self.parts.values())
        if received > MAX_RESTORE_BYTES:
            raise TransferError("that upload is larger than a backup file can be")
        return len(self.parts)

    @property
    def complete(self) -> bool:
        return len(self.parts) == self.chunks

    def assembled(self) -> bytes:
        """The whole file, or a refusal naming what is missing."""
        if not self.complete:
            missing = sorted(set(range(self.chunks)) - set(self.parts))
            raise TransferError(
                f"the upload is incomplete — {len(missing)} of {self.chunks} "
                f"chunk(s) never arrived (first missing: {missing[0]})"
            )
        blob = b"".join(self.parts[i] for i in range(self.chunks))
        if self.size and len(blob) != self.size:
            # Not paranoia: a mismatch here means the file the browser read is not
            # the file that arrived, and decrypting it would fail later with a much
            # less useful message.
            raise TransferError(
                f"the upload arrived as {len(blob)} bytes but was announced as "
                f"{self.size} — it is incomplete or was changed on the way"
            )
        return blob


class TransferStore:
    """Every transfer in flight for this Home Assistant."""

    def __init__(self) -> None:
        self._downloads: dict[str, _Download] = {}
        self._uploads: dict[str, _Upload] = {}

    # ------------------------------------------------------------- bookkeeping

    def _sweep(self) -> None:
        now = time.time()
        for store in (self._downloads, self._uploads):
            for key, transfer in list(store.items()):
                if now - transfer.touched > _IDLE_TIMEOUT:
                    _LOGGER.debug("Dropping abandoned transfer %s", key[:8])
                    store.pop(key, None)

    # --------------------------------------------------------------- download

    def start_download(self, payload: bytes, filename: str) -> dict[str, Any]:
        self._sweep()
        download = _Download(payload=payload, filename=filename)
        self._downloads[download.transfer_id] = download
        return {
            "download_id": download.transfer_id,
            "filename": filename,
            "size": len(payload),
            "chunk_size": VAULT_CHUNK_BYTES,
            "chunks": download.chunks,
        }

    def download_chunk(self, download_id: str, index: int) -> dict[str, Any]:
        download = self._downloads.get(download_id)
        if download is None:
            raise TransferError(
                "that download has expired — create the backup again"
            )
        return download.chunk(index)

    def end_download(self, download_id: str) -> None:
        """Free the buffer. Not an error if it is already gone."""
        self._downloads.pop(download_id, None)

    # ----------------------------------------------------------------- upload

    def start_upload(self, filename: str, size: int, chunks: int) -> str:
        self._sweep()
        if size > MAX_RESTORE_BYTES:
            raise TransferError(
                f"that file is {size // (1024 * 1024)} MB — too large to be an ekey "
                "backup. Nothing has been read."
            )
        if chunks < 1 or chunks > 4096:
            raise TransferError("that upload declares an implausible number of chunks")
        upload = _Upload(filename=filename, size=size, chunks=chunks)
        self._uploads[upload.transfer_id] = upload
        return upload.transfer_id

    def upload_chunk(self, upload_id: str, index: int, data: str) -> dict[str, Any]:
        upload = self._get_upload(upload_id)
        received = upload.add(index, data)
        return {"received": received, "chunks": upload.chunks}

    def uploaded_bytes(self, upload_id: str) -> bytes:
        return self._get_upload(upload_id).assembled()

    def upload_name(self, upload_id: str) -> str:
        return self._get_upload(upload_id).filename

    def abort_upload(self, upload_id: str) -> None:
        """Drop an upload. Not an error if it is already gone."""
        self._uploads.pop(upload_id, None)

    def _get_upload(self, upload_id: str) -> _Upload:
        upload = self._uploads.get(upload_id)
        if upload is None:
            raise TransferError(
                "that upload has expired — choose the file again"
            )
        return upload


def async_get_transfers(hass) -> TransferStore:
    """The one transfer store for this HA run."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get(_TRANSFERS_CACHE_KEY)
    if store is None:
        store = TransferStore()
        domain_data[_TRANSFERS_CACHE_KEY] = store
    return store
