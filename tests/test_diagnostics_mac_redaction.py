"""MAC addresses embedded in values must not leak through exports (#282)."""

from __future__ import annotations

import json

import pytest

from custom_components.ajax.diagnostics import redact_ajax_data

MAC = "A1B2C3D4E5F6"


def test_channel_token_keeps_its_shape_but_not_the_mac() -> None:
    data = {"channels": [{"id": f"TOK-{MAC}-0"}, {"id": "TOK-00000"}, {"id": "TOK-0"}]}
    out = redact_ajax_data(data)
    assert [c["id"] for c in out["channels"]] == ["TOK-**MAC_1**-0", "TOK-00000", "TOK-0"]


@pytest.mark.parametrize("text", [MAC, MAC.lower(), "A1:B2:C3:D4:E5:F6", "a1-b2-c3-d4-e5-f6"])
def test_every_notation_is_masked_with_the_same_placeholder(text: str) -> None:
    out = redact_ajax_data([f"x {text} y", f"TOK-{MAC}-0"])
    assert out == ["x **MAC_1** y", "TOK-**MAC_1**-0"]


def test_two_cameras_stay_distinguishable() -> None:
    out = redact_ajax_data([f"TOK-{MAC}-0", "TOK-112233445566-0", f"TOK-{MAC}-1"])
    assert out == ["TOK-**MAC_1**-0", "TOK-**MAC_2**-0", "TOK-**MAC_1**-1"]


def test_known_mac_is_masked_even_glued_to_hex_and_as_a_key() -> None:
    data = {"macAddress": "A1:B2:C3:D4:E5:F6", "name": f"ab{MAC}cd", "map": {MAC: 1}}
    out = redact_ajax_data(data)
    assert out["macAddress"] == "**REDACTED**"
    assert MAC not in json.dumps(out).upper()
    assert out["map"] == {"**MAC_1**": 1}


def test_ordinary_values_are_untouched_and_input_is_not_modified() -> None:
    data = {"id": "30A1B2C3", "t": "2026-11-18T10:36:39", "ts": 1795152150123, "ok": True, "none": None}
    copy = dict(data)
    assert redact_ajax_data(data) == copy
    assert data == copy
