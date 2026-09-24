"""Extract the Ajax TOTP secret from a Google Authenticator export link.

Google Authenticator never shows a secret; its only way out is "Transfer
accounts", a QR code holding ``otpauth-migration://offline?data=<base64>``,
where ``data`` is a protobuf ``MigrationPayload``. Users who only have that
app would otherwise have to disable and re-enable 2FA to see the key.

The format is small and stable, so it is decoded by hand rather than pulling
in ``protobuf``:

    MigrationPayload { repeated OtpParameters otp_parameters = 1; ... }
    OtpParameters    { bytes secret = 1; string name = 2; string issuer = 3;
                       ...; OtpType type = 6; ... }   # type: 1 HOTP, 2 TOTP

Nothing here logs or returns anything but the chosen secret.
"""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlsplit

MIGRATION_SCHEME = "otpauth-migration://"

_OTP_TYPE_HOTP = 1


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _fields(buf: bytes) -> list[tuple[int, int | bytes]]:
    """Split a protobuf message into (field number, value) pairs."""
    out: list[tuple[int, int | bytes]] = []
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        number, wire = key >> 3, key & 0x07
        if wire == 0:
            value, pos = _read_varint(buf, pos)
            out.append((number, value))
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            if pos + length > len(buf):
                raise ValueError("truncated field")
            out.append((number, buf[pos : pos + length]))
            pos += length
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return out


def secret_from_migration_uri(uri: str) -> str:
    """Return the Base32 secret of the Ajax TOTP account in an export link.

    A link holding a single TOTP account yields it; with several, the one
    whose issuer or name mentions Ajax is chosen. Anything else — no account,
    or several candidates with none (or more than one) clearly Ajax — raises
    ``ValueError`` rather than guess.
    """
    data = parse_qs(urlsplit(uri.strip()).query).get("data", [""])[0]
    # parse_qs turns a raw "+" into a space; the export percent-encodes it,
    # but a hand-copied link may not.
    data = data.replace(" ", "+")
    if not data:
        raise ValueError("no data parameter")
    payload = base64.b64decode(data + "=" * (-len(data) % 4), validate=False)

    accounts: list[tuple[bytes, str]] = []
    for number, value in _fields(payload):
        if number != 1 or not isinstance(value, bytes):
            continue
        secret = b""
        label = ""
        otp_type = 0
        for field, item in _fields(value):
            if field == 1 and isinstance(item, bytes):
                secret = item
            elif field in (2, 3) and isinstance(item, bytes):
                label += " " + item.decode("utf-8", "replace")
            elif field == 6 and isinstance(item, int):
                otp_type = item
        if secret and otp_type != _OTP_TYPE_HOTP:
            accounts.append((secret, label))

    if len(accounts) != 1:
        accounts = [acc for acc in accounts if "ajax" in acc[1].lower()]
    if len(accounts) != 1:
        raise ValueError("no single Ajax account in export")
    return base64.b32encode(accounts[0][0]).decode("ascii").rstrip("=")
