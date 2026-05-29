"""Bootstrap mixin for ``AjaxDataCoordinator``.

Owns the lazy initialisations the coordinator runs after the REST login
is complete: build the in-memory account, persist / restore the
SSE-or-SQS-discovered smart locks, and stand up the optional SSE and
AWS-SQS managers when the user has configured them.

The optional-import guards for SSE and SQS live in this module — the
coordinator does not need them anywhere else now that the init
methods have moved.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .models import AjaxAccount, AjaxSmartLock

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.storage import Store

    from .api import AjaxRestApi
    from .sqs_manager import SQSManager
    from .sse_manager import SSEManager

# Optional SQS support — the coordinator __init__ leaves the manager at
# None when the package is missing; the mixin keeps the guard local so
# nothing else needs to know about the optional dependency.
SQS_AVAILABLE = False
_SQSManager: type | None = None
_AjaxSQSClient: type | None = None
try:
    from .sqs_client import AjaxSQSClient as _AjaxSQSClient
    from .sqs_manager import SQSManager as _SQSManager

    SQS_AVAILABLE = True
except ImportError:
    pass

# Optional SSE support (proxy mode).
SSE_AVAILABLE = False
_SSEManager: type | None = None
_AjaxSSEClient: type | None = None
try:
    from .sse_client import AjaxSSEClient as _AjaxSSEClient
    from .sse_manager import SSEManager as _SSEManager

    SSE_AVAILABLE = True
except ImportError:
    pass

# Storage schema version for SSE/SQS-discovered smart locks. Bumped only
# when ``_async_migrate_smart_locks_store`` knows how to upgrade the
# payload — see that method for the migration contract.
SMART_LOCK_STORE_VERSION = 1

_LOGGER = logging.getLogger(__name__)


class AjaxBootstrapMixin:
    """Coordinator mixin: account init, smart-lock persistence, SSE/SQS bootstrap."""

    # Host attributes — provided by the coordinator __init__.
    if TYPE_CHECKING:
        account: AjaxAccount | None
        api: AjaxRestApi
        hass: HomeAssistant
        config_entry: ConfigEntry | None
        sqs_manager: SQSManager | None
        sse_manager: SSEManager | None
        _aws_access_key_id: str | None
        _aws_secret_access_key: str | None
        _queue_name: str | None
        _sqs_initialized: bool
        _sse_url: str | None
        _sse_initialized: bool
        _smart_lock_store: Store[Any]

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    async def _async_init_account(self) -> None:
        """Initialise the in-memory account from the login response.

        Ajax has no /user endpoint, so we synthesise the account from
        what the login already returned (``user_id`` + ``email``).
        """
        self.account = AjaxAccount(
            user_id=self.api.user_id or "",
            name=self.api.email.split("@")[0] if self.api.email else "Unknown",
            email=self.api.email or "",
        )
        # Log only a truncated user_id — the full value is PII (and doubles as a
        # session token in proxy mode), so keep it out of shared INFO logs.
        _LOGGER.info(
            "Initialized account for %s (user_id: %s…)",
            self.account.name,
            self.account.user_id[:8],
        )

    # ------------------------------------------------------------------
    # Smart-lock store
    # ------------------------------------------------------------------

    @staticmethod
    async def _async_migrate_smart_locks_store(
        old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Migrate the smart-locks ``Store[Any]`` payload across schema versions.

        Current schema (v1) is ``space_id -> list[smart_lock_dict]``.
        This is a placeholder: the day we bump ``SMART_LOCK_STORE_VERSION``
        the upgrade code lives here.
        """
        _LOGGER.debug(
            "Smart-lock store migration: %s.%s -> %s",
            old_major_version,
            old_minor_version,
            SMART_LOCK_STORE_VERSION,
        )
        return old_data or {}

    async def _async_save_smart_locks(self) -> None:
        """Persist SSE/SQS-discovered smart locks to storage."""
        if not self.account:
            return
        data: dict[str, list[dict[str, Any]]] = {}
        for space_id, space in self.account.spaces.items():
            locks = []
            for sl in space.smart_locks.values():
                # Only persist SSE/SQS-discovered locks (no raw_data from API).
                if not sl.raw_data:
                    locks.append(
                        {
                            "id": sl.id,
                            "name": sl.name,
                            "is_locked": sl.is_locked,
                            "is_door_open": sl.is_door_open,
                            "last_changed_by": sl.last_changed_by,
                        }
                    )
            if locks:
                data[space_id] = locks
        if data:
            await self._smart_lock_store.async_save(data)

    async def _async_restore_smart_locks(self) -> None:
        """Restore SSE/SQS-discovered smart locks from storage."""
        if not self.account:
            return
        data = await self._smart_lock_store.async_load()
        if not data or not isinstance(data, dict):
            return
        count = 0
        for space_id, locks in data.items():
            space = self.account.spaces.get(space_id)
            if not space:
                continue
            for lock_data in locks:
                sl_id = lock_data.get("id")
                if not sl_id or sl_id in space.smart_locks:
                    continue
                smart_lock = AjaxSmartLock(
                    id=sl_id,
                    name=lock_data.get("name", f"Smart Lock {sl_id[:6]}"),
                    space_id=space_id,
                )
                smart_lock.is_locked = lock_data.get("is_locked")
                smart_lock.is_door_open = lock_data.get("is_door_open")
                smart_lock.last_changed_by = lock_data.get("last_changed_by")
                space.smart_locks[sl_id] = smart_lock
                count += 1
        if count:
            _LOGGER.info("Restored %d SSE-discovered smart lock(s) from storage", count)

    # ------------------------------------------------------------------
    # AWS SQS (direct mode)
    # ------------------------------------------------------------------

    async def _async_init_sqs(self) -> None:
        """Initialise AWS SQS for real-time events (optional).

        SQS provides real-time event notifications (<1 s latency) that
        trigger immediate REST API updates:
        * SQS events → instant state updates.
        * REST polling → baseline updates every 30 s as fallback.

        Requires ``aiobotocore`` + AWS credentials. On failure the
        integration falls back to REST-only mode without raising.
        """
        if not SQS_AVAILABLE:
            _LOGGER.info("AWS SQS not available (aiobotocore not installed). Using REST API polling only.")
            self._sqs_initialized = True  # Mark as "initialized" to prevent retries.
            return

        if not self._aws_access_key_id or not self._aws_secret_access_key or not self._queue_name:
            _LOGGER.debug("AWS credentials not configured. Using REST API polling only.")
            self._sqs_initialized = True
            return

        entry_id = self.config_entry.entry_id if self.config_entry else "unknown"
        sqs_issue = f"sqs_init_failed_{entry_id}"

        try:
            _LOGGER.info("Initializing AWS SQS for real-time events...")

            assert _AjaxSQSClient is not None  # Validated by SQS_AVAILABLE above.
            assert _SQSManager is not None
            sqs_client = _AjaxSQSClient(
                aws_access_key_id=self._aws_access_key_id,
                aws_secret_access_key=self._aws_secret_access_key,
                queue_name=self._queue_name,
                hass_loop=self.hass.loop,
            )

            self.sqs_manager = _SQSManager(
                coordinator=self,
                sqs_client=sqs_client,
            )

            ha_language = self.hass.config.language or "en"
            lang_map = {"fr": "fr", "es": "es", "en": "en"}
            sqs_language = lang_map.get(ha_language[:2], "en")
            self.sqs_manager.set_language(sqs_language)

            success = await self.sqs_manager.start()

            if success:
                _LOGGER.info("✓ AWS SQS initialized successfully - Real-time events enabled!")
                ir.async_delete_issue(self.hass, DOMAIN, sqs_issue)
            else:
                _LOGGER.warning("Failed to start SQS - Falling back to REST API polling only")
                self.sqs_manager = None
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    sqs_issue,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="sqs_init_failed",
                    translation_placeholders={"error": "Manager failed to start"},
                )

        except Exception as err:
            _LOGGER.warning(
                "Failed to initialize AWS SQS: %s - Using REST API polling only",
                err,
            )
            self.sqs_manager = None
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                sqs_issue,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="sqs_init_failed",
                translation_placeholders={"error": str(err)},
            )
        finally:
            self._sqs_initialized = True

    # ------------------------------------------------------------------
    # SSE (proxy mode)
    # ------------------------------------------------------------------

    async def _async_init_sse(self) -> None:
        """Initialise SSE for real-time events (proxy mode).

        SSE provides real-time event notifications via the proxy server
        when the user is not on the direct/SQS path. Failure falls back
        to REST-only mode without raising.
        """
        if not SSE_AVAILABLE:
            _LOGGER.info("SSE not available (module not loaded). Using REST API polling only.")
            self._sse_initialized = True
            return

        if not self._sse_url:
            _LOGGER.debug("SSE URL not configured. Using REST API polling only.")
            self._sse_initialized = True
            return

        if not self.api.session_token:
            _LOGGER.warning("No session token available for SSE. Using REST API polling only.")
            self._sse_initialized = True
            return

        entry_id = self.config_entry.entry_id if self.config_entry else "unknown"
        sse_issue = f"sse_init_failed_{entry_id}"

        try:
            _LOGGER.info("Initializing SSE for real-time events...")

            assert _AjaxSSEClient is not None  # Validated by SSE_AVAILABLE above.
            assert _SSEManager is not None
            sse_client = _AjaxSSEClient(
                sse_url=self._sse_url,
                session_token=self.api.session_token or "",
                callback=lambda event: None,  # Set by the manager once started.
                hass_loop=self.hass.loop,
                user_id=self.api.user_id,
                verify_ssl=self.api.verify_ssl,
                token_provider=lambda: self.api.session_token,
            )

            self.sse_manager = _SSEManager(
                coordinator=self,
                sse_client=sse_client,
            )

            ha_language = self.hass.config.language or "en"
            lang_map = {"fr": "fr", "es": "es", "en": "en"}
            sse_language = lang_map.get(ha_language[:2], "en")
            self.sse_manager.set_language(sse_language)

            success = await self.sse_manager.start()

            if success:
                _LOGGER.info("✓ SSE initialized successfully - Real-time events enabled!")
                ir.async_delete_issue(self.hass, DOMAIN, sse_issue)
            else:
                _LOGGER.warning("Failed to start SSE - Falling back to REST API polling only")
                self.sse_manager = None
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    sse_issue,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="sse_init_failed",
                    translation_placeholders={"error": "Manager failed to start"},
                )

        except Exception as err:
            _LOGGER.warning(
                "Failed to initialize SSE: %s - Using REST API polling only",
                err,
            )
            self.sse_manager = None
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                sse_issue,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="sse_init_failed",
                translation_placeholders={"error": str(err)},
            )
        finally:
            self._sse_initialized = True
