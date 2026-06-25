"""Tests for the pure parser helpers on the coordinator.

These helpers translate raw Ajax API strings into domain values. They
are pure functions (no `self.account` / `self.api` access) so we can
drive them through a tiny stub instead of standing up a full
DataUpdateCoordinator.

A regression in any of these silently breaks state synchronisation —
e.g. `_parse_security_state` returning `ARMED` for `DISARMED_NIGHT_MODE_OFF`
(because "DISARMED" contains "ARMED") used to flip the panel state.
"""

from __future__ import annotations

import pytest

from custom_components.ajax._coordinator_state import AjaxStateUpdaterMixin
from custom_components.ajax.coordinator import AjaxDataCoordinator
from custom_components.ajax.models import DeviceType, SecurityState


@pytest.fixture
def parser() -> AjaxStateUpdaterMixin:
    """Build a bare mixin instance — no super().__init__ needed for parsers."""
    return AjaxStateUpdaterMixin()


# ---------------------------------------------------------------------------
# _parse_security_state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The Ajax API ships compound state strings: "DISARMED" must NOT match
        # the ARMED branch even though "ARMED" appears as a substring — this is
        # the regression that historically flipped the panel state.
        ("DISARMED", SecurityState.DISARMED),
        ("DISARMED_NIGHT_MODE_OFF", SecurityState.DISARMED),
        ("DISARMED_NIGHT_MODE_ON", SecurityState.DISARMED),
        ("ARMED", SecurityState.ARMED),
        ("ARMED_NIGHT_MODE_OFF", SecurityState.ARMED),
        ("ARMED_NIGHT_MODE_ON", SecurityState.NIGHT_MODE),
        ("NIGHT_MODE", SecurityState.NIGHT_MODE),
        ("NIGHT_MODE_ON", SecurityState.NIGHT_MODE),
        ("PARTIALLY_ARMED", SecurityState.PARTIALLY_ARMED),
        ("armed", SecurityState.ARMED),  # case-insensitive
        ("disarmed", SecurityState.DISARMED),
    ],
)
def test_parse_security_state_normalises_compound_strings(
    parser: AjaxStateUpdaterMixin, raw: str, expected: SecurityState
) -> None:
    assert parser._parse_security_state(raw) is expected


@pytest.mark.parametrize("bogus", [None, 42, "", "SOMETHING_ELSE", []])
def test_parse_security_state_returns_none_for_unrecognised(parser: AjaxStateUpdaterMixin, bogus: object) -> None:
    assert parser._parse_security_state(bogus) is SecurityState.NONE


# ---------------------------------------------------------------------------
# _parse_device_type
# ---------------------------------------------------------------------------


def test_parse_device_type_resolves_known_alias(parser: AjaxStateUpdaterMixin) -> None:
    assert parser._parse_device_type("MotionProtect") is DeviceType.MOTION_DETECTOR


def test_parse_device_type_resolves_range_extender_variants(parser: AjaxStateUpdaterMixin) -> None:
    """Rex 1 / Rex 2 raw types map explicitly, not via the 'extender' substring fallback (#167)."""
    assert parser._parse_device_type("RangeExtender") is DeviceType.REPEATER
    assert parser._parse_device_type("RangeExtender2") is DeviceType.REPEATER


def test_parse_device_type_strips_trailing_braces(parser: AjaxStateUpdaterMixin) -> None:
    """The Ajax API occasionally ships values like 'wire_input_mt {\\n}\\n' — must still resolve."""
    assert parser._parse_device_type("wire_input_mt {\n}\n") is DeviceType.WIRE_INPUT


def test_parse_device_type_returns_unknown_for_garbage(parser: AjaxStateUpdaterMixin) -> None:
    assert parser._parse_device_type("zzz-unknown-zzz") is DeviceType.UNKNOWN


def test_parse_device_type_returns_unknown_for_non_string(parser: AjaxStateUpdaterMixin) -> None:
    assert parser._parse_device_type(None) is DeviceType.UNKNOWN  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _parse_door_state_from_wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "external_state,wiring_details,expected",
    [
        ("OK", None, False),
        ("TRIGGERED", None, True),
        (None, None, False),
        ("OK", {"wiringSchemeType": "TWO_EOL", "contactTwoDetails": {"contactState": "OK"}}, False),
        ("OK", {"wiringSchemeType": "TWO_EOL", "contactTwoDetails": {"contactState": "ALARM"}}, True),
        ("OK", {"wiringSchemeType": "ONE_EOL", "contactDetails": {"contactState": "ALARM"}}, True),
        ("OK", {"wiringSchemeType": "ONE_EOL", "contactDetails": {"contactState": "OK"}}, False),
        ("OK", {"wiringSchemeType": "NO_EOL", "contactState": "ALARM"}, True),
        ("OK", {"wiringSchemeType": "NO_EOL", "contactState": "OK"}, False),
        # Unknown scheme: fall back to external_state
        ("TRIGGERED", {"wiringSchemeType": "WAT"}, True),
    ],
)
def test_parse_door_state_from_wiring_handles_all_eol_schemas(
    external_state: str | None, wiring_details: dict | None, expected: bool
) -> None:
    assert AjaxDataCoordinator._parse_door_state_from_wiring(external_state, wiring_details) is expected
