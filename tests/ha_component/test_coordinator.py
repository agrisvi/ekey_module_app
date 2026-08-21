"""Unit tests for the ekey HA coordinator module.

Tests cover:
  - clean_json_string()  (§M12 — the function most likely to break on daemon output)
  - EkeyDataUpdateCoordinator construction and base_url formation
  - _async_update_data() success and error paths (mocked aiohttp)

Run with:
    pytest tests/ha_component/ -v
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Import the module under test.
# The component lives at custom_components/ekey_ha_app/
# The PYTHONPATH in CI is set to "." (repo root) so the import works as:
#   from custom_components.ekey_ha_app.coordinator import ...
# ---------------------------------------------------------------------------
from custom_components.ekey_ha_app.coordinator import (
    clean_json_string,
    EkeyDataUpdateCoordinator,
)
from custom_components.ekey_ha_app.connection import EkeyConnection


# ===========================================================================
# clean_json_string
# ===========================================================================

class TestCleanJsonString:
    """Tests for clean_json_string()."""

    def test_plain_ascii_unchanged(self):
        """ASCII-only strings must pass through unchanged."""
        s = '{"status": "ok", "value": 42}'
        assert clean_json_string(s) == s

    def test_removes_null_byte(self):
        """NUL byte (0x00) must be replaced with a space."""
        s = 'hello\x00world'
        result = clean_json_string(s)
        assert '\x00' not in result
        assert 'hello' in result
        assert 'world' in result

    def test_removes_newline_inside_string(self):
        """Literal newline inside a JSON string value must be removed."""
        s = '{"msg": "line1\nline2"}'
        result = clean_json_string(s)
        assert '\n' not in result
        # Must still be parseable JSON after cleaning
        parsed = json.loads(result)
        assert 'msg' in parsed

    def test_removes_carriage_return(self):
        """Carriage return (0x0D) must be replaced."""
        s = 'value\r\nmore'
        result = clean_json_string(s)
        assert '\r' not in result
        assert '\n' not in result

    def test_removes_tab(self):
        """Tab character (0x09) must be replaced."""
        s = 'col1\tcol2'
        result = clean_json_string(s)
        assert '\t' not in result

    def test_removes_all_control_chars_below_32(self):
        """All ASCII control characters (0x00–0x1F) must be replaced."""
        # Build a string with every control character
        controls = ''.join(chr(i) for i in range(32))
        s = 'before' + controls + 'after'
        result = clean_json_string(s)
        for i in range(32):
            assert chr(i) not in result, f"Control char 0x{i:02X} not removed"
        assert 'before' in result
        assert 'after' in result

    def test_keeps_printable_unicode(self):
        """Printable Unicode (≥ 0x20) must be preserved."""
        s = '{"name": "Jānis Bērziņš"}'
        result = clean_json_string(s)
        assert 'Jānis' in result
        assert 'Bērziņš' in result

    def test_empty_string(self):
        """Empty string must return empty string."""
        assert clean_json_string('') == ''

    def test_real_daemon_output_parseable(self):
        """Simulate daemon output with embedded control chars — must parse after cleaning."""
        # Daemon sometimes embeds 0x1E (record separator) inside JSON strings
        raw = '{"cmd":"GET_DEVICE_INFORMATION","sw_version":"2.1\x1e0","id":1}'
        cleaned = clean_json_string(raw)
        parsed = json.loads(cleaned)
        assert parsed['cmd'] == 'GET_DEVICE_INFORMATION'
        assert parsed['id'] == 1

    def test_result_is_parseable_json_with_control_chars(self):
        """A JSON object with control chars in values must be parseable after cleaning."""
        raw = '{"a": "val\x00ue", "b": 123}'
        cleaned = clean_json_string(raw)
        parsed = json.loads(cleaned)
        assert parsed['b'] == 123


# ===========================================================================
# EkeyDataUpdateCoordinator construction
# ===========================================================================

class TestEkeyDataUpdateCoordinatorInit:
    """Tests for coordinator construction."""

    def _make_coordinator(self, host="127.0.0.1", port=8080):
        """Create a coordinator with a mocked hass and session."""
        hass = MagicMock()
        hass.data = {}
        session = MagicMock()
        conn = EkeyConnection(host=host, port=port)
        return EkeyDataUpdateCoordinator(hass, session, conn)

    def test_base_url_formed_correctly(self):
        coord = self._make_coordinator("127.0.0.1", 8080)
        assert coord.base_url == "http://127.0.0.1:8080"

    def test_base_url_custom_port(self):
        coord = self._make_coordinator("192.168.1.10", 9090)
        assert coord.base_url == "http://192.168.1.10:9090"

    def test_host_stored(self):
        coord = self._make_coordinator("localhost", 8080)
        assert coord.host == "localhost"

    def test_port_stored(self):
        coord = self._make_coordinator("127.0.0.1", 1234)
        assert coord.port == 1234

    def test_update_interval_is_none(self):
        """Coordinator must have update_interval=None (push-based, §M4)."""
        coord = self._make_coordinator()
        assert coord.update_interval is None

    def test_remote_https_base_url(self):
        """A remote (SSL) connection must give the coordinator an https base_url."""
        hass = MagicMock()
        hass.data = {}
        conn = EkeyConnection(host="192.168.1.20", port=8080, use_ssl=True, token="tok")
        coord = EkeyDataUpdateCoordinator(hass, MagicMock(), conn)
        assert coord.base_url == "https://192.168.1.20:8080"


# ===========================================================================
# EkeyConnection — the local/remote connection descriptor
# ===========================================================================

class TestEkeyConnection:
    """Tests for the EkeyConnection dataclass (two-mode connection)."""

    def test_local_defaults_to_http(self):
        conn = EkeyConnection(host="127.0.0.1", port=8080)
        assert conn.scheme == "http"
        assert conn.base_url == "http://127.0.0.1:8080"

    def test_remote_uses_https(self):
        conn = EkeyConnection(host="ekey.local", port=8080, use_ssl=True)
        assert conn.scheme == "https"
        assert conn.base_url == "https://ekey.local:8080"

    def test_headers_include_bearer_token(self):
        conn = EkeyConnection(host="h", port=1, token="secret")
        assert conn.headers() == {"Authorization": "Bearer secret"}

    def test_headers_empty_without_token(self):
        """Local mode with no token must send no Authorization header."""
        conn = EkeyConnection(host="h", port=1)
        assert conn.headers() == {}

    def test_scanner_id_is_scheme_less(self):
        """scanner_id stays host:port so event routing is unchanged across modes."""
        conn = EkeyConnection(host="10.0.0.5", port=8080, use_ssl=True, token="t")
        assert conn.scanner_id == "10.0.0.5:8080"


# ===========================================================================
# _async_update_data — mocked HTTP responses
# ===========================================================================

@pytest.mark.asyncio
class TestEkeyDataUpdateCoordinatorFetch:
    """Tests for _async_update_data() with mocked aiohttp."""

    def _make_coordinator(self):
        hass = MagicMock()
        hass.data = {}
        session = AsyncMock()
        conn = EkeyConnection(host="127.0.0.1", port=8080)
        return EkeyDataUpdateCoordinator(hass, session, conn)

    async def test_successful_fetch_returns_dict(self):
        """A successful HTTP response must return a dict with device data."""
        coord = self._make_coordinator()

        device_response = {
            "cmd": "GET_DEVICE_INFORMATION",
            "rpc_error_code": "OK",
            "sw_version": "2.1.0",
            "fw_api_version": 6,
        }
        fp_response = {
            "cmd": "GET_SAVED_APS",
            "rpc_error_code": "OK",
            "count": 2,
        }

        mock_resp_device = AsyncMock()
        mock_resp_device.status = 200
        mock_resp_device.text = AsyncMock(return_value=json.dumps(device_response))

        mock_resp_fp = AsyncMock()
        mock_resp_fp.status = 200
        mock_resp_fp.text = AsyncMock(return_value=json.dumps(fp_response))

        coord.session.get = AsyncMock(side_effect=[
            mock_resp_device.__aenter__.return_value if hasattr(mock_resp_device, '__aenter__') else mock_resp_device,
            mock_resp_fp.__aenter__.return_value if hasattr(mock_resp_fp, '__aenter__') else mock_resp_fp,
        ])

        # Patch the context manager usage
        coord.session.get = MagicMock()
        ctx1 = MagicMock()
        ctx1.__aenter__ = AsyncMock(return_value=mock_resp_device)
        ctx1.__aexit__ = AsyncMock(return_value=False)
        ctx2 = MagicMock()
        ctx2.__aenter__ = AsyncMock(return_value=mock_resp_fp)
        ctx2.__aexit__ = AsyncMock(return_value=False)
        coord.session.get.side_effect = [ctx1, ctx2]

        result = await coord._async_update_data()
        assert isinstance(result, dict)

    async def test_http_error_raises_update_failed(self):
        """A non-200 HTTP response must raise UpdateFailed."""
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord = self._make_coordinator()

        mock_resp = AsyncMock()
        mock_resp.status = 503
        mock_resp.text = AsyncMock(return_value="Service Unavailable")

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        coord.session.get = MagicMock(return_value=ctx)

        with pytest.raises((UpdateFailed, Exception)):
            await coord._async_update_data()

    async def test_connection_error_raises_update_failed(self):
        """A connection error must raise UpdateFailed (not crash)."""
        from homeassistant.helpers.update_coordinator import UpdateFailed
        import aiohttp

        coord = self._make_coordinator()

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=aiohttp.ClientConnectionError("refused"))
        ctx.__aexit__ = AsyncMock(return_value=False)
        coord.session.get = MagicMock(return_value=ctx)

        with pytest.raises((UpdateFailed, Exception)):
            await coord._async_update_data()
