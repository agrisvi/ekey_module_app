"""SSE (Server-Sent Events) listener for ekey scanner events."""
import logging
import asyncio
import json
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from datetime import timedelta

from .connection import EkeyConnection
from .const import (
    API_EVENTS,
    EVENT_FINGER_TOUCH,
    EVENT_FINGERPRINT_MATCHED,
    EVENT_FINGERPRINT_NOT_MATCHED,
    EVENT_ENROLLMENT_STATE,
    MATCH_OK,
    MATCH_NOT_OK,
)
from .util import clean_json_string

_LOGGER = logging.getLogger(__name__)


class EkeySSEListener:
    """Listen to ekey scanner Server-Sent Events stream."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        conn: EkeyConnection,
        entry_id: str | None = None,
    ) -> None:
        """Initialize the SSE listener.

        ``conn`` supplies the scheme/host/port and auth header so the event
        stream works over both HTTP (daemon) and HTTPS + token (ESP32).
        """
        self.hass = hass
        self.session = session
        self.conn = conn
        self.host = conn.host
        self.port = conn.port
        self.entry_id = entry_id
        self.scanner_id = conn.scanner_id
        self.url = f"{conn.base_url}{API_EVENTS}"
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._last_event_time = None
        self._connection_lost_reported = False

    async def start(self) -> None:
        """Start listening to SSE events."""
        _LOGGER.info("Starting ekey SSE listener for %s", self.url)
        
        while not self._stop_event.is_set():
            try:
                async with self.session.get(
                    self.url,
                    headers=self.conn.headers(),
                    timeout=aiohttp.ClientTimeout(total=None),
                ) as response:
                    if response.status != 200:
                        _LOGGER.error("SSE connection failed with status %s", response.status)
                        await asyncio.sleep(5)
                        continue
                    
                    _LOGGER.info("SSE connection established")
                    self._connection_lost_reported = False

                    # The daemon emits one JSON object per event as a single
                    # SSE line: ``data: {json}\n\n``. Parse line-by-line, which
                    # is robust to braces embedded inside JSON string values
                    # (unlike the previous brace-depth counter).
                    try:
                        async for line_bytes in response.content:
                            if self._stop_event.is_set():
                                break

                            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")

                            if not line:
                                # Blank line: SSE event boundary, nothing to do.
                                continue

                            if line.startswith(":"):
                                # SSE comment / keep-alive.
                                self._last_event_time = asyncio.get_event_loop().time()
                                continue

                            if not line.startswith("data:"):
                                # Field we don't consume (e.g. "event:", "id:").
                                continue

                            json_str = line[len("data:"):].strip()
                            if not json_str:
                                continue

                            _LOGGER.debug("SSE data line: %d bytes", len(json_str))

                            try:
                                data = json.loads(json_str)
                            except json.JSONDecodeError as err:
                                _LOGGER.debug(
                                    "Failed to parse SSE JSON: %s. Raw: %r", err, json_str[:200]
                                )
                                try:
                                    data = json.loads(clean_json_string(json_str))
                                except Exception as clean_err:
                                    _LOGGER.debug(
                                        "Could not parse even after cleaning: %s", clean_err
                                    )
                                    continue

                            await self._handle_event(data)
                            self._last_event_time = asyncio.get_event_loop().time()

                        else:
                            # Iterator exhausted without a stop request: server
                            # closed the stream. Fall through to reconnect.
                            _LOGGER.debug("SSE connection closed by server")
                    except asyncio.CancelledError:
                        raise
            
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                if not self._connection_lost_reported:
                    _LOGGER.warning("Lost connection to ekey scanner: %s", err)
                    self.hass.bus.async_fire("ekey_connection_lost")
                    self._connection_lost_reported = True
                
                await asyncio.sleep(5)  # Retry after 5 seconds
            
            except Exception as err:
                _LOGGER.exception("Unexpected error in SSE listener: %s", err)
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop listening to SSE events."""
        _LOGGER.info("Stopping ekey SSE listener")
        self._stop_event.set()
        
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _handle_event(self, data: dict[str, Any]) -> None:
        """Handle incoming SSE event."""
        cmd = data.get("cmd")
        
        if cmd == "NOTIFY_FINGER_TOUCH":
            _LOGGER.info(">>> Finger touch detected - waiting for match result")
            self.hass.bus.async_fire(EVENT_FINGER_TOUCH, data)
        
        elif cmd == "NOTIFY_AP_MATCHED":
            apid = data.get("apid", "")
            apfar = data.get("apfar", -1)
            apfar_desc = data.get("apfar_desc", "")
            
            # Tag every event with the originating scanner so handlers act only on it.
            origin = {"entry_id": self.entry_id, "scanner_id": self.scanner_id}

            if apfar == MATCH_OK:
                _LOGGER.info(">>> Match result: POSITIVE - APID=%s (%s) on %s", apid[:8] + "..." if apid else "none", apfar_desc, self.scanner_id)
                self.hass.bus.async_fire(EVENT_FINGERPRINT_MATCHED, {
                    "apid": apid,
                    "apfar": apfar,
                    "apfar_desc": apfar_desc,
                    **origin,
                })
                # Fire event to turn on green LED (setSignalingState match positive)
                _LOGGER.debug(">>> Sending setSignalingState(match positive) - Green LED on %s", self.scanner_id)
                self.hass.bus.async_fire("ekey_flash_green_led", dict(origin))
            else:
                _LOGGER.info(">>> Match result: NEGATIVE - %s on %s", apfar_desc, self.scanner_id)
                self.hass.bus.async_fire(EVENT_FINGERPRINT_NOT_MATCHED, {
                    "apfar": apfar,
                    "apfar_desc": apfar_desc,
                    **origin,
                })
                # Fire event to turn on red LED (setSignalingState match negative)
                _LOGGER.debug(">>> Sending setSignalingState(match negative) - Red LED on %s", self.scanner_id)
                self.hass.bus.async_fire("ekey_flash_red_led", dict(origin))
        
        elif cmd == "NOTIFY_AP_ENROLL_STATE":
            apid = data.get("apid", "")
            enstat = data.get("enstat", -1)
            entryc = data.get("entryc", 0)
            ennumtpl = data.get("ennumtpl", 0)
            
            # Log enrollment progress
            from .const import ENROLLMENT_STATES, ENROLL_STATE_FINISHED_SUCCESS
            state_name = ENROLLMENT_STATES.get(enstat, f"unknown({enstat})")
            
            _LOGGER.info(
                "Enrollment progress: APID=%s, state=%s, tries=%d, templates=%d",
                apid[:8] + "...", state_name, entryc, ennumtpl
            )
            
            # Fire enrollment state event for real-time UI updates
            _LOGGER.debug(">>> Firing EVENT_ENROLLMENT_STATE for APID=%s with data: %s", apid[:8] + "..." if apid else "None", data)
            self.hass.bus.async_fire(EVENT_ENROLLMENT_STATE, data)
            
            # Check for completion states
            if enstat == ENROLL_STATE_FINISHED_SUCCESS:
                _LOGGER.info("Enrollment completed successfully for APID=%s", apid[:8] + "...")
                self.hass.bus.async_fire("ekey_enrollment_complete", {
                    "apid": apid,
                    "success": True,
                    "entryc": entryc,
                    "ennumtpl": ennumtpl,
                })
            elif enstat >= 50:  # Any failure state (50, 60, 70)
                _LOGGER.warning("Enrollment failed for APID=%s with state=%s", apid[:8] + "...", state_name)
                self.hass.bus.async_fire("ekey_enrollment_complete", {
                    "apid": apid,
                    "success": False,
                    "state": state_name,
                    "enstat": enstat,
                })
        
        else:
            _LOGGER.debug("Received unknown event: %s", cmd)
