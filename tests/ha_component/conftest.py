"""Shared fixtures for the ekey component tests.

Constructing a real Home Assistant ``DataUpdateCoordinator`` outside the full HA
runtime triggers ``homeassistant.helpers.frame.report_usage(...)`` from
``DataUpdateCoordinator.__init__``, which raises ``RuntimeError: Frame helper not
set up`` because that helper only exists during normal startup. These are unit
tests built on a mocked ``hass``, so the telemetry call is neutralised for the
whole session; it only emits usage warnings and has no bearing on what is tested.
"""
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _neutralize_frame_helper():
    """Stop the HA frame helper from raising when no real runtime is set up."""
    with patch("homeassistant.helpers.frame.report_usage"):
        yield
