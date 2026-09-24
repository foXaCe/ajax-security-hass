"""Tests for the Google Authenticator export decoder."""

from __future__ import annotations

import base64
from urllib.parse import quote

import pytest

from custom_components.ajax._totp_migration import secret_from_migration_uri
from custom_components.ajax.config_flow import AjaxConfigFlow

AJAX_KEY = "JBSWY3DPEHPK3PXP"
OTHER_KEY = "GEZDGNBVGY3TQOJQ"


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, payload: bytes | int) -> bytes:
    if isinstance(payload, int):
        return _varint(number << 3) + _varint(payload)
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _account(key: str, name: str, issuer: str, otp_type: int = 2) -> bytes:
    return (
        _field(1, base64.b32decode(key))
        + _field(2, name.encode())
        + _field(3, issuer.encode())
        + _field(4, 1)  # SHA1
        + _field(5, 1)  # six digits
        + _field(6, otp_type)
    )


def _uri(*accounts: bytes, encode: bool = True) -> str:
    payload = b"".join(_field(1, acc) for acc in accounts) + _field(2, 1) + _field(3, 1)
    data = base64.b64encode(payload).decode()
    return "otpauth-migration://offline?data=" + (quote(data, safe="") if encode else data)


def test_single_account() -> None:
    assert secret_from_migration_uri(_uri(_account(AJAX_KEY, "me@x.com", "Ajax"))) == AJAX_KEY


def test_picks_the_ajax_account_among_several() -> None:
    uri = _uri(
        _account(OTHER_KEY, "me@x.com", "Google"),
        _account(AJAX_KEY, "me@x.com", "Ajax Systems"),
    )
    assert secret_from_migration_uri(uri) == AJAX_KEY


def test_matches_ajax_in_the_name_too() -> None:
    uri = _uri(_account(OTHER_KEY, "github", "GitHub"), _account(AJAX_KEY, "Ajax:me@x.com", ""))
    assert secret_from_migration_uri(uri) == AJAX_KEY


def test_raw_plus_signs_survive() -> None:
    """A hand-copied link keeps '+' unescaped; parse_qs would make it a space."""
    uri = _uri(_account(AJAX_KEY, "me", "Ajax"), encode=False)
    assert secret_from_migration_uri(uri) == AJAX_KEY


def test_hotp_accounts_are_ignored() -> None:
    uri = _uri(_account(OTHER_KEY, "x", "Other", otp_type=1), _account(AJAX_KEY, "me", "Something"))
    assert secret_from_migration_uri(uri) == AJAX_KEY


@pytest.mark.parametrize(
    "uri",
    [
        "otpauth-migration://offline",
        "otpauth-migration://offline?data=",
        "otpauth-migration://offline?data=%%%",
    ],
)
def test_malformed_links_raise(uri: str) -> None:
    with pytest.raises(ValueError):
        secret_from_migration_uri(uri)


def test_ambiguous_export_raises_instead_of_guessing() -> None:
    uri = _uri(_account(OTHER_KEY, "a", "Google"), _account(AJAX_KEY, "b", "GitHub"))
    with pytest.raises(ValueError):
        secret_from_migration_uri(uri)


def test_two_ajax_accounts_raise() -> None:
    uri = _uri(_account(OTHER_KEY, "a", "Ajax"), _account(AJAX_KEY, "b", "Ajax"))
    with pytest.raises(ValueError):
        secret_from_migration_uri(uri)


def test_truncated_payload_raises() -> None:
    data = base64.b64encode(_field(1, _account(AJAX_KEY, "me", "Ajax"))[:-5]).decode()
    with pytest.raises(ValueError):
        secret_from_migration_uri("otpauth-migration://offline?data=" + quote(data, safe=""))


def test_config_flow_accepts_the_export_link() -> None:
    assert AjaxConfigFlow._clean_totp_secret(_uri(_account(AJAX_KEY, "me", "Ajax"))) == AJAX_KEY


def test_config_flow_maps_a_bad_export_to_invalid_secret() -> None:
    with pytest.raises(ValueError, match="invalid_totp_secret"):
        AjaxConfigFlow._clean_totp_secret("otpauth-migration://offline?data=")
