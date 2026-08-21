"""Shared utilities for the ekey Home Assistant App integration."""

from __future__ import annotations

import json
from typing import Any


def clean_json_string(text: str) -> str:
    """Remove control characters from a JSON string.

    The daemon sometimes embeds literal control characters (including newlines)
    inside JSON string values, which is invalid JSON. This replaces ALL control
    characters (ASCII < 32) with a space to make the JSON parseable. Valid JSON
    whitespace outside strings is ignored by the parser anyway.
    """
    return "".join(char if ord(char) >= 32 else " " for char in text)


def split_json_documents(text: str) -> list[Any]:
    """Decode a body that holds several JSON documents back to back.

    The daemon answers some scanner commands with more than one object in a single
    response. Starting an enrollment is the clearest case: the library emits the
    ``START_AP_ENROLL`` reply and then deliberately re-enables the
    ``NOTIFY_AP_ENROLL_STATE`` block so the same state also reaches the event
    stream, and both land in the one per-request accumulator. The body is then
    valid NDJSON but invalid JSON, and ``json.loads`` rejects it with "Extra data"
    even though the command succeeded on the scanner.

    Returns the documents in order, or an empty list if the first one will not
    decode. A trailing fragment is dropped rather than failing the whole read: half
    a notification is worth less than the reply that precedes it.
    """
    decoder = json.JSONDecoder()
    documents: list[Any] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        if index >= len(text):
            break
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError:
            break
        documents.append(value)
    return documents


def pick_rpc_reply(documents: list[Any]) -> Any:
    """Choose the RPC reply out of a multi-document response.

    Every RPC reply carries ``rpc_error_code``; no notification does. Selecting on
    that rather than taking the first document is the part that matters: a scanner
    refusal arrives as HTTP 200 with ``rpc_error_code: "Error"``, so reading a
    notification here would report a refused enrollment as a successful one.

    Returns ``None`` when there is nothing to choose from.
    """
    for document in documents:
        if isinstance(document, dict) and "rpc_error_code" in document:
            return document
    return documents[0] if documents else None
