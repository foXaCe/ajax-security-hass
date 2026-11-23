"""MAC addresses must not leak through the integration's own log lines (#285)."""

from __future__ import annotations

import logging
import pkgutil
import re
from pathlib import Path

import pytest

import custom_components.ajax as ajax
from custom_components.ajax import _log_redaction
from custom_components.ajax._log_redaction import MAC_LOG_FILTER

MAC = "A1B2C3D4E5F6"
UUID4 = "123e4567-e89b-42d3-a456-426614174000"
UUID1 = f"123e4567-e89b-12d3-a456-{MAC}"
LOGGER_NAME = "custom_components.ajax.sensor"


@pytest.fixture(autouse=True)
def _fresh_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_log_redaction, "_placeholders", {})


def _all_logger_names() -> list[str]:
    return [ajax.__name__] + [m.name for m in pkgutil.walk_packages(ajax.__path__, f"{ajax.__name__}.")]


def test_every_module_logger_is_filtered() -> None:
    names = _all_logger_names()
    assert LOGGER_NAME in names
    assert "custom_components.ajax.api" in names
    assert "custom_components.ajax.devices.video_edge" in names
    assert [n for n in names if MAC_LOG_FILTER not in logging.getLogger(n).filters] == []


def test_every_logger_is_created_under_its_module_name() -> None:
    root = Path(ajax.__path__[0])
    covered = {"__name__"} | {f'"{name}"' for name in _all_logger_names()}
    for source in root.rglob("*.py"):
        for arg in re.findall(r"logging\.getLogger\(([^)]*)\)", source.read_text(encoding="utf-8")):
            if source.name != "_log_redaction.py":
                assert arg in covered, f"{source.name}: {arg}"


def _log(caplog: pytest.LogCaptureFixture, msg: str, *args: object) -> str:
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        logging.getLogger(LOGGER_NAME).debug(msg, *args)
    return caplog.records[-1].getMessage()


def test_reported_line_is_masked(caplog: pytest.LogCaptureFixture) -> None:
    out = _log(caplog, "Created video edge sensor '%s' for: %s", f"record_mode_TOK-{MAC}-0", "NVR AI")
    assert out == "Created video edge sensor 'record_mode_TOK-**MAC_1**-0' for: NVR AI"


def test_rtsp_path_and_raw_event_are_masked_with_the_same_placeholder(caplog: pytest.LogCaptureFixture) -> None:
    rtsp = _log(caplog, "rtsp://***:***@%s/%s", "cam", f"{MAC.lower()}-0_m")
    raw = _log(caplog, "SSE raw event data: %s", {"id": f"TOK-{MAC}-0", "mac": "A1:B2:C3:D4:E5:F6"})
    assert rtsp == "rtsp://***:***@cam/**MAC_1**-0_m"
    assert raw == "SSE raw event data: {'id': 'TOK-**MAC_1**-0', 'mac': '**MAC_1**'}"


def test_two_cameras_stay_distinguishable(caplog: pytest.LogCaptureFixture) -> None:
    assert _log(caplog, "%s %s", MAC, "112233445566") == "**MAC_1** **MAC_2**"


def test_random_uuid_is_kept_but_a_uuid_holding_a_mac_is_not(caplog: pytest.LogCaptureFixture) -> None:
    assert _log(caplog, "event %s", UUID4) == f"event {UUID4}"
    assert MAC not in _log(caplog, "event %s", UUID1)
    # A channel token that starts like a UUID must not hide the MAC that follows.
    assert MAC not in _log(caplog, "channel %s", f"{UUID4}-{MAC}-0")


def test_other_records_are_left_untouched(caplog: pytest.LogCaptureFixture) -> None:
    assert _log(caplog, "%s opened at %d", "30A1B2C3", 1795152150123) == "30A1B2C3 opened at 1795152150123"
    assert caplog.records[-1].args == ("30A1B2C3", 1795152150123)


def test_literal_percent_survives_masking(caplog: pytest.LogCaptureFixture) -> None:
    assert _log(caplog, "%s at 50%%", MAC) == "**MAC_1** at 50%"


def test_traceback_is_masked(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        try:
            raise ValueError(f"bad channel TOK-{MAC}-0")
        except ValueError:
            logging.getLogger(LOGGER_NAME).exception("failed")
    assert "bad channel TOK-**MAC_1**-0" in caplog.text
    assert MAC not in caplog.text


def test_a_broken_log_call_is_not_swallowed_nor_raised_by_the_filter() -> None:
    record = logging.LogRecord(LOGGER_NAME, logging.DEBUG, __file__, 1, "%d", ("x",), None)
    assert MAC_LOG_FILTER.filter(record) is True


def test_placeholders_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_log_redaction, "_MAX_PLACEHOLDERS", 2)
    out = _log_redaction.mask_macs("000000000001 000000000002 000000000003 000000000001")
    assert out == "**MAC_1** **MAC_2** **MAC** **MAC_1**"
