"""Data update coordinator for ekey scanner."""
import logging
import asyncio
import json
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .connection import EkeyConnection
from .const import (
    API_DEVICE,
    API_FINGERPRINTS,
    API_FINGERPRINTS_ENROLL,
    API_FINGERPRINTS_ENROLL_CONFIRM,
    API_FINGERPRINTS_ENROLL_QUIT,
    API_LED,
    API_LED_BRIGHTNESS,
)
from .util import clean_json_string, pick_rpc_reply, split_json_documents

_LOGGER = logging.getLogger(__name__)


class EkeyDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching ekey scanner data."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        conn: "EkeyConnection",
    ) -> None:
        """Initialize the coordinator.

        ``conn`` carries the scheme/host/port and the auth header, so this works
        unchanged for both the local daemon (HTTP) and a remote ESP32 (HTTPS +
        token). ``host``/``port``/``base_url`` are exposed for callers that still
        reference them (e.g. button.py, services.py).
        """
        self.session = session
        self.conn = conn
        self.host = conn.host
        self.port = conn.port
        self.base_url = conn.base_url

        super().__init__(
            hass,
            _LOGGER,
            name="ekey Scanner",
            update_interval=None,  # Disable automatic polling - refresh only on demand
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from ekey scanner."""
        try:
            _LOGGER.debug("Fetching device info from %s", self.base_url)
            
            # Get device information (required)
            device_info = await self._get_device_info()
            
            # Small delay to avoid overwhelming daemon with concurrent requests
            await asyncio.sleep(0.5)
            
            # Get enrolled fingerprints (optional - may timeout if scanner is busy)
            try:
                fingerprints = await self._get_fingerprints()
            except UpdateFailed as err:
                _LOGGER.warning("Failed to get fingerprints (scanner may be busy): %s", err)
                fingerprints = {"num_aps": 0, "aps": []}
            
            _LOGGER.debug("Successfully fetched scanner data")
            
            return {
                "device": device_info,
                "fingerprints": fingerprints,
            }
        
        except aiohttp.ClientError as err:
            _LOGGER.error("Error communicating with ekey scanner at %s: %s", self.base_url, err)
            raise UpdateFailed(f"Error communicating with ekey scanner: {err}") from err
        except asyncio.TimeoutError as err:
            _LOGGER.error("Timeout connecting to ekey scanner at %s", self.base_url)
            raise UpdateFailed(f"Timeout connecting to ekey scanner") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching ekey scanner data: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def _get_device_info(self) -> dict[str, Any]:
        """Get device information from scanner."""
        url = f"{self.base_url}{API_DEVICE}"
        
        _LOGGER.debug("GET %s", url)
        
        try:
            async with self.session.get(url, headers=self.conn.headers(), timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                        _LOGGER.debug("Device info response: %s", data)
                        return data
                    except Exception as json_err:
                        # Fallback: try parsing raw text
                        raw_text = await response.text()
                        _LOGGER.error("JSON parse error (will try cleaning): %s", json_err)
                        _LOGGER.debug("Raw response text: %r", raw_text[:500])
                        
                        try:
                            # Clean control characters and try again
                            cleaned_text = clean_json_string(raw_text)
                            _LOGGER.debug("Cleaned text: %r", cleaned_text[:500])
                            data = json.loads(cleaned_text)
                            _LOGGER.warning("Successfully parsed after cleaning control characters")
                            return data
                        except Exception as clean_err:
                            _LOGGER.error("Failed to parse even after cleaning: %s", clean_err)
                            raise UpdateFailed(f"Failed to parse device info JSON: {json_err}")
                
                error_text = await response.text()
                _LOGGER.error("Device info request failed with status %s: %s", response.status, error_text)
                raise UpdateFailed(f"Device info request failed with status {response.status}")
        except asyncio.TimeoutError:
            _LOGGER.error("Timeout getting device info from %s", url)
            raise

    async def _get_fingerprints(self) -> dict[str, Any]:
        """Get list of enrolled fingerprints."""
        url = f"{self.base_url}{API_FINGERPRINTS}"
        
        _LOGGER.debug("GET %s", url)
        
        try:
            async with self.session.get(url, headers=self.conn.headers(), timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                        _LOGGER.debug("Fingerprints response: %s", data)
                        return data
                    except Exception as json_err:
                        # Fallback: try parsing raw text with control character cleaning
                        raw_text = await response.text()
                        _LOGGER.error("JSON parse error getting fingerprints (will try cleaning): %s", json_err)
                        _LOGGER.debug("Raw fingerprints text: %r", raw_text[:500])
                        
                        try:
                            # Clean control characters and try again
                            cleaned_text = clean_json_string(raw_text)
                            _LOGGER.debug("Cleaned fingerprints text: %r", cleaned_text[:500])
                            data = json.loads(cleaned_text)
                            _LOGGER.warning("Successfully parsed fingerprints after cleaning control characters")
                            return data
                        except Exception as clean_err:
                            _LOGGER.error("Failed to parse fingerprints even after cleaning: %s", clean_err)
                            raise UpdateFailed(f"Failed to parse fingerprints JSON: {json_err}")
                
                # 504 means scanner timeout - this can happen if scanner is busy
                if response.status == 504:
                    error_text = await response.text()
                    _LOGGER.debug("Scanner timeout getting fingerprints: %s", error_text)
                    raise UpdateFailed(f"Scanner timeout (504)")
                
                error_text = await response.text()
                _LOGGER.error("Fingerprints request failed with status %s: %s", response.status, error_text)
                raise UpdateFailed(f"Fingerprints request failed with status {response.status}")
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout getting fingerprints from %s", url)
            raise UpdateFailed("Request timeout")

    async def enroll_fingerprint(self, apid: str) -> dict[str, Any]:
        """Start fingerprint enrollment.

        The finger number is purely controller-side bookkeeping (tracked in
        ``pending_enrollments``); the daemon identifies the enrollment solely by
        the APID. ``id`` is a request-correlation id and is fixed at 1.
        """
        url = f"{self.base_url}{API_FINGERPRINTS_ENROLL}"
        payload = {
            "Apid": apid,
            "Apnot": 1,  # Enable notifications
            "id": 1,
        }
        
        _LOGGER.info("POST %s with payload: %s", url, payload)
        
        try:
            async with self.session.post(
                url,
                json=payload,
                headers=self.conn.headers(),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if response.status == 200:
                    try:
                        return await response.json()
                    except Exception as json_err:
                        raw_text = await response.text()
                        _LOGGER.error("JSON parse error in enrollment response: %s", json_err)
                        _LOGGER.debug("Raw enrollment response: %r", raw_text[:500])
                        
                        cleaned_text = clean_json_string(raw_text)
                        try:
                            # Clean and retry
                            data = json.loads(cleaned_text)
                            _LOGGER.warning("Enrollment response parsed after cleaning")
                            return data
                        except Exception as clean_err:
                            _LOGGER.debug("Enrollment parse after cleaning: %s", clean_err)

                        # Starting an enrollment answers with TWO documents: the
                        # START_AP_ENROLL reply and the NOTIFY_AP_ENROLL_STATE the
                        # library re-emits for the event stream. That is NDJSON, not
                        # JSON, so the strict parse above fails with "Extra data"
                        # while the scanner has already started. Pick the reply out —
                        # by rpc_error_code, which no notification carries, so a
                        # refusal is still seen as a refusal.
                        reply = pick_rpc_reply(split_json_documents(cleaned_text))
                        if reply is not None:
                            return reply

                        _LOGGER.error("Failed enrollment parse after cleaning: %s", json_err)
                        raise UpdateFailed(f"Failed to parse enrollment JSON: {json_err}")
                
                # Handle scanner timeout
                if response.status == 504:
                    error_text = await response.text()
                    _LOGGER.warning("Scanner timeout starting enrollment: %s", error_text)
                    raise UpdateFailed(f"Scanner timeout (504)")
                
                error_text = await response.text()
                _LOGGER.error("Enroll request failed with status %s: %s", response.status, error_text)
                raise UpdateFailed(f"Enroll request failed with status {response.status}")
        except asyncio.TimeoutError:
            _LOGGER.error("Timeout starting enrollment at %s", url)
            raise UpdateFailed("Request timeout")

    async def confirm_enrollment(self, apid: str) -> dict[str, Any]:
        """Confirm fingerprint enrollment.

        Note: Success is determined by SSE event (enstat=40), not this HTTP response.
        This method may timeout but enrollment can still complete successfully.
        """
        url = f"{self.base_url}{API_FINGERPRINTS_ENROLL_CONFIRM}"
        payload = {
            "Apid": apid,
            "id": 1,
        }
        
        _LOGGER.info("POST %s with payload: %s", url, payload)
        
        try:
            async with self.session.post(
                url,
                json=payload,
                headers=self.conn.headers(),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                if response.status == 200:
                    try:
                        return await response.json()
                    except Exception as json_err:
                        raw_text = await response.text()
                        _LOGGER.error("JSON parse error in confirm enrollment: %s", json_err)
                        try:
                            cleaned_text = clean_json_string(raw_text)
                            return json.loads(cleaned_text)
                        except Exception as clean_err:
                            _LOGGER.error("Failed to parse confirm response: %s", clean_err)
                            raise UpdateFailed(f"Failed to parse JSON: {json_err}")
                
                if response.status == 504:
                    error_text = await response.text()
                    _LOGGER.warning("Scanner timeout on confirm (not critical - will check SSE for completion): %s", error_text)
                    raise UpdateFailed(f"Scanner timeout (504) - monitoring via SSE")
                
                error_text = await response.text()
                _LOGGER.error("Confirm enrollment failed with status %s: %s", response.status, error_text)
                raise UpdateFailed(f"Confirm enrollment failed with status {response.status}")
        
        except asyncio.TimeoutError:
            _LOGGER.warning("Timeout confirming enrollment (not critical - will check SSE for completion)")
            raise UpdateFailed("Request timeout - monitoring via SSE")

    async def quit_enrollment(self, apid: str) -> dict[str, Any]:
        """Abort fingerprint enrollment."""
        url = f"{self.base_url}{API_FINGERPRINTS_ENROLL_QUIT}"
        payload = {
            "Apid": apid,
            "id": 1,
        }
        
        _LOGGER.info("POST %s with payload: %s", url, payload)
        
        try:
            async with self.session.post(
                url,
                json=payload,
                headers=self.conn.headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status == 200:
                    try:
                        return await response.json()
                    except Exception as json_err:
                        raw_text = await response.text()
                        _LOGGER.error("JSON parse error in quit enrollment: %s", json_err)
                        try:
                            cleaned_text = clean_json_string(raw_text)
                            return json.loads(cleaned_text)
                        except Exception as clean_err:
                            _LOGGER.error("Failed to parse quit response: %s", clean_err)
                            raise UpdateFailed(f"Failed to parse JSON: {json_err}")
                
                if response.status == 504:
                    error_text = await response.text()
                    _LOGGER.warning("Scanner timeout quitting enrollment: %s", error_text)
                    raise UpdateFailed(f"Scanner timeout (504)")
                
                error_text = await response.text()
                _LOGGER.error("Quit enrollment failed with status %s: %s", response.status, error_text)
                raise UpdateFailed(f"Quit enrollment failed with status {response.status}")
        except asyncio.TimeoutError:
            _LOGGER.error("Timeout quitting enrollment at %s", url)
            raise UpdateFailed("Request timeout")

    async def delete_fingerprint(self, apid: str) -> bool:
        """Delete a fingerprint."""
        url = f"{self.base_url}{API_FINGERPRINTS}/{apid}"

        _LOGGER.info("DELETE %s", url)
        
        async with self.session.delete(url, headers=self.conn.headers(), timeout=aiohttp.ClientTimeout(total=10)) as response:
            success = response.status == 200
            if success:
                # Wait 3 seconds for scanner to finish processing before refreshing
                _LOGGER.debug("Waiting 3 seconds before refreshing after fingerprint deletion")
                await asyncio.sleep(3)
                # Refresh coordinator data after successful deletion
                await self.async_request_refresh()
            return success

    async def set_led_brightness(self, brightness: int) -> dict[str, Any]:
        """Set LED brightness (0-100)."""
        url = f"{self.base_url}{API_LED_BRIGHTNESS}"
        payload = {
            "Brightness": brightness,
            "id": 1,
        }
        
        _LOGGER.info("POST %s with payload: %s", url, payload)
        
        async with self.session.post(
            url,
            json=payload,
            headers=self.conn.headers(),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status == 200:
                try:
                    return await response.json()
                except Exception as json_err:
                    raw_text = await response.text()
                    _LOGGER.error("JSON parse error in set brightness: %s", json_err)
                    try:
                        cleaned_text = clean_json_string(raw_text)
                        return json.loads(cleaned_text)
                    except Exception as clean_err:
                        _LOGGER.error("Failed to parse brightness response: %s", clean_err)
                        raise UpdateFailed(f"Failed to parse JSON: {json_err}")
            raise UpdateFailed(f"Set brightness failed with status {response.status}")

    async def set_led_state(self, state: int) -> dict[str, Any]:
        """Set LED state (4=green, 5=red, 6=red/green)."""
        url = f"{self.base_url}{API_LED}"
        payload = {
            "State": state,
            "id": 1,
        }
        
        _LOGGER.info("POST %s with payload: %s", url, payload)
        
        async with self.session.post(
            url,
            json=payload,
            headers=self.conn.headers(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            if response.status == 200:
                try:
                    return await response.json()
                except Exception as json_err:
                    raw_text = await response.text()
                    _LOGGER.error("JSON parse error in set LED state: %s", json_err)
                    try:
                        cleaned_text = clean_json_string(raw_text)
                        return json.loads(cleaned_text)
                    except Exception as clean_err:
                        _LOGGER.error("Failed to parse LED state response: %s", clean_err)
                        raise UpdateFailed(f"Failed to parse JSON: {json_err}")
            raise UpdateFailed(f"Set LED state failed with status {response.status}")

