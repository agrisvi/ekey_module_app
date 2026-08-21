"""A tiny stand-in for ``aiohttp.ClientSession``.

Hand-rolled rather than pulled from ``aioresponses`` so the suite runs on a bare
``pytest`` + ``homeassistant`` install, which is what CI and the developer's
Windows box actually have. It implements exactly the surface
:class:`~custom_components.ekey_ha_app.api.EkeyAppClient` uses: ``request`` and
``get`` as async context managers, ``status``, ``headers`` and ``text()``.

Responses are registered per ``(method, path)``. Recording the requests as they
arrive is deliberate: several behaviours worth testing are about *what was sent* —
that a PUT carries the whole user list, that a delete is followed by a re-read of
the sensor's list before anything is dropped.
"""
from __future__ import annotations

import json
from typing import Any


class FakeResponse:
    """One canned HTTP response."""

    def __init__(self, status: int, body: Any = None, headers: dict | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        if body is None:
            self._text = ""
        elif isinstance(body, str):
            self._text = body
        else:
            self._text = json.dumps(body)

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


class FakeSession:
    """Routes ``(method, path)`` to canned responses and records every call."""

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        # value may be a FakeResponse, a list of them (consumed in order, the last
        # one repeating), or a callable(body) -> FakeResponse.
        self.routes: dict[tuple[str, str], Any] = dict(routes or {})
        self.calls: list[dict[str, Any]] = []

    # -- registration -------------------------------------------------------

    def add(self, method: str, path: str, status: int = 200, body: Any = None,
            headers: dict | None = None) -> None:
        self.routes[(method.upper(), path)] = FakeResponse(status, body, headers)

    def add_sequence(self, method: str, path: str, responses: list[FakeResponse]) -> None:
        """Answer differently on successive calls (the last answer repeats)."""
        self.routes[(method.upper(), path)] = list(responses)

    # -- the ClientSession surface -----------------------------------------

    def request(self, method, url, **kwargs):
        return self._dispatch(method, url, kwargs)

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, kwargs)

    def _dispatch(self, method, url, kwargs):
        path = self._path_of(url)
        self.calls.append(
            {
                "method": method.upper(),
                "path": path,
                "url": url,
                "json": kwargs.get("json"),
                "headers": kwargs.get("headers") or {},
            }
        )
        route = self.routes.get((method.upper(), path))
        if route is None:
            return FakeResponse(404, {"error": "endpoint not found"})
        if isinstance(route, list):
            response = route[0] if len(route) == 1 else route.pop(0)
            return response
        if callable(route):
            return route(kwargs.get("json"))
        return route

    @staticmethod
    def _path_of(url: str) -> str:
        """Strip scheme/host and the query string, mirroring the backend routers."""
        without_scheme = url.split("://", 1)[-1]
        path = "/" + without_scheme.split("/", 1)[1] if "/" in without_scheme else "/"
        return path.split("?", 1)[0]

    # -- assertions helpers -------------------------------------------------

    def paths(self, method: str | None = None) -> list[str]:
        """Paths called, in order — for asserting call sequences."""
        return [
            call["path"]
            for call in self.calls
            if method is None or call["method"] == method.upper()
        ]

    def last_json(self, method: str, path: str) -> Any:
        """The body of the most recent matching call."""
        for call in reversed(self.calls):
            if call["method"] == method.upper() and call["path"] == path:
                return call["json"]
        return None
