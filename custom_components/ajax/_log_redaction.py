"""Mask MAC addresses in the integration's log lines.

NVR channel ids embed the camera MAC (``<token>-<MAC>-0``) and end up in entity
keys, RTSP paths and raw events. Masking at the logger level covers them all,
including log calls added later (#285).
"""

from __future__ import annotations

import logging
import pkgutil
import re
import threading

# Also used by diagnostics.py.
MAC_PATTERN = (
    r"(?<![0-9A-Fa-f])"
    r"(?:[0-9A-Fa-f]{12}|[0-9A-Fa-f]{2}(?P<sep>[:-])(?:[0-9A-Fa-f]{2}(?P=sep)){4}[0-9A-Fa-f]{2})"
    r"(?![0-9A-Fa-f])"
)

# The last group of a UUID looks like a MAC. It is kept for the versions that
# never hold one, as log lines carry many of them and they help to follow events.
_UUID_WITHOUT_MAC = (
    r"(?<![0-9A-Fa-f-])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[34578][0-9A-Fa-f]{3}-[89ABab][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f-])"
)
_UUID_OR_MAC = re.compile(f"(?P<uuid>{_UUID_WITHOUT_MAC})|{MAC_PATTERN}")

# Anything with 12 hexadecimal digits is taken for a MAC: keep the table bounded.
_MAX_PLACEHOLDERS = 256
_placeholders: dict[str, str] = {}
_lock = threading.Lock()  # SQS logs from its own thread


def normalize_mac(value: str) -> str:
    """Return the MAC as 12 lowercase hexadecimal digits."""
    return value.replace(":", "").replace("-", "").lower()


def _replace(match: re.Match[str]) -> str:
    text = match.group(0)
    if match.group("uuid"):
        return text
    mac = normalize_mac(text)
    with _lock:
        if mac in _placeholders:
            return _placeholders[mac]
        if len(_placeholders) >= _MAX_PLACEHOLDERS:
            return "**MAC**"
        return _placeholders.setdefault(mac, f"**MAC_{len(_placeholders) + 1}**")


def mask_macs(text: str) -> str:
    """Replace each MAC with a placeholder that stays the same until restart."""
    return _UUID_OR_MAC.sub(_replace, text)


class MacLogFilter(logging.Filter):
    """Rewrite the records that hold a MAC address; never drop one."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Mask the message and the traceback of ``record``."""
        try:
            message = record.getMessage()
        except (TypeError, ValueError, KeyError):
            return True  # bad format arguments: let logging report them
        masked = mask_macs(message)
        if masked != message:
            record.msg = masked
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = mask_macs(logging.Formatter().formatException(record.exc_info))
        return True


MAC_LOG_FILTER = MacLogFilter()


def install_log_filter(package: str, path: list[str]) -> None:
    """Attach the filter to the logger of ``package`` and of each of its modules.

    A filter set on the package logger alone would not see the records of its
    children, so every module logger is prepared here, whether the module is imported yet or not.
    """
    logging.getLogger(package).addFilter(MAC_LOG_FILTER)
    for module in pkgutil.iter_modules(path):
        name = f"{package}.{module.name}"
        if module.ispkg:
            install_log_filter(name, [f"{base}/{module.name}" for base in path])
        else:
            logging.getLogger(name).addFilter(MAC_LOG_FILTER)
