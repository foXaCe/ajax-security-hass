"""Ajax SSE (Server-Sent Events) client for real-time events via proxy.

This client connects to an SSE endpoint provided by the Ajax proxy server
and receives real-time events. It's an alternative to the SQS client for
users connecting via proxy mode.

SSE Protocol:
- HTTP connection stays open
- Server sends events in format: "event: type\ndata: {json}\n\n"
- Client automatically reconnects on disconnection
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .const import INTEGRATION_VERSION

_LOGGER = logging.getLogger(__name__)


class AjaxSSEClient:
    """SSE client for Ajax real-time events via proxy."""

    # Reconnection settings
    RECONNECT_DELAY = 5  # seconds between reconnection attempts
    MAX_RECONNECT_DELAY = 60  # max delay between attempts
    CONNECTION_TIMEOUT = 30  # timeout for initial connection
    AUTH_FAILURE_THRESHOLD = 3  # persistent 401/403 count before escalating

    def __init__(
        self,
        sse_url: str,
        session_token: str,
        callback: Callable[[dict[str, Any]], Awaitable[None] | None],
        hass_loop: asyncio.AbstractEventLoop | None = None,
        user_id: str | None = None,
        verify_ssl: bool = True,
        token_provider: Callable[[], str | None] | None = None,
        on_auth_failure: Callable[[], None] | None = None,
    ):
        """Initialize the SSE client.

        Args:
            sse_url: URL of the SSE endpoint (from proxy login response)
            session_token: Session token for authentication (initial value)
            callback: Function to call when an event is received
            hass_loop: Home Assistant event loop for thread-safe callbacks
            user_id: User ID for proxy rate limiting (X-User-Id header)
            verify_ssl: Verify SSL certificates (set False for self-signed certs)
            token_provider: Callable that returns the current session token.
                If provided, used on each reconnect to get the latest token.
        """
        self.sse_url = sse_url
        self.session_token = session_token
        self._token_provider = token_provider
        self.user_id = user_id
        self.verify_ssl = verify_ssl
        self._callback = callback
        self._hass_loop = hass_loop
        self._running = False
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._reconnect_delay = self.RECONNECT_DELAY
        self._auth_failures = 0
        self._on_auth_failure = on_auth_failure
        # Hold strong refs on callback tasks so they are not GC'd mid-flight
        self._pending_callback_tasks: set[asyncio.Task] = set()

    async def start(self) -> bool:
        """Start receiving SSE events.

        Returns:
            True if started successfully
        """
        if self._running:
            _LOGGER.warning("SSE client already running")
            return True

        self._running = True
        self._task = asyncio.create_task(self._receive_loop())
        _LOGGER.info("SSE client started for %s", self.sse_url)
        return True

    async def stop(self) -> None:
        """Stop receiving SSE events."""
        _LOGGER.info("Stopping SSE client...")
        self._running = False

        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

        _LOGGER.info("SSE client stopped")

    async def _receive_loop(self) -> None:
        """Main receive loop - connects to SSE and processes events."""
        while self._running:
            try:
                await self._connect_and_receive()
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.error("SSE connection error: %s", err)

            if self._running:
                _LOGGER.info("SSE reconnecting in %d seconds...", self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                # Exponential backoff
                self._reconnect_delay = min(self._reconnect_delay * 2, self.MAX_RECONNECT_DELAY)

    async def _connect_and_receive(self) -> None:
        """Connect to SSE endpoint and receive events."""
        if not self._session or self._session.closed:
            # Create session with SSL verification setting
            if self.verify_ssl:
                connector = None
            else:
                connector = aiohttp.TCPConnector(ssl=False)
                _LOGGER.warning("SSE: SSL certificate verification disabled")
            self._session = aiohttp.ClientSession(connector=connector)

        # Always get the latest token on reconnect
        if self._token_provider:
            fresh_token = self._token_provider()
            if fresh_token:
                self.session_token = fresh_token

        headers = {
            "X-Session-Token": self.session_token,
            "X-Client-Version": INTEGRATION_VERSION,
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        # Add user ID for proxy rate limiting by user (not just IP)
        if self.user_id:
            headers["X-User-Id"] = self.user_id

        _LOGGER.debug("Connecting to SSE: %s", self.sse_url)

        async with self._session.get(
            self.sse_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=None,  # No total timeout for SSE
                connect=self.CONNECTION_TIMEOUT,
                sock_read=300,  # Detect dead proxy after 5 min of silence
            ),
        ) as response:
            if response.status != 200:
                _LOGGER.error("SSE connection failed: HTTP %d", response.status)
                # Escalate backoff on failure so we don't hammer the server.
                self._reconnect_delay = min(self._reconnect_delay * 2, self.MAX_RECONNECT_DELAY)
                if response.status in (401, 403):
                    self._auth_failures += 1
                    if self._auth_failures >= self.AUTH_FAILURE_THRESHOLD and self._on_auth_failure:
                        _LOGGER.error(
                            "SSE auth failed %d times, signalling reauth",
                            self._auth_failures,
                        )
                        try:
                            self._on_auth_failure()
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.error("on_auth_failure callback error: %s", err)
                        self._running = False
                return

            _LOGGER.info("SSE connected successfully")
            self._reconnect_delay = self.RECONNECT_DELAY  # Reset on successful connect
            self._auth_failures = 0  # Reset on successful connect

            # Cap per-line size to prevent memory blow-up on malformed streams
            MAX_LINE_BYTES = 1 * 1024 * 1024  # 1 MiB

            # Read SSE stream
            event_type: str | None = None
            event_data: list[str] = []

            async for line_bytes in response.content:
                if not self._running:
                    break

                if len(line_bytes) > MAX_LINE_BYTES:
                    _LOGGER.warning("SSE line exceeded %d bytes, skipping", MAX_LINE_BYTES)
                    continue

                line = line_bytes.decode("utf-8", errors="replace").rstrip("\n\r")

                if not line:
                    # Empty line = end of event
                    if event_data:
                        await self._process_event(event_type, "\n".join(event_data))
                        event_type = None
                        event_data = []
                elif line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    event_data.append(line[5:].strip())
                elif line.startswith(":"):
                    # Comment/keepalive, ignore
                    _LOGGER.debug("SSE keepalive received")
                elif line.startswith("{"):
                    # Raw JSON line (Julien's proxy format)
                    await self._process_event(None, line)

    async def _process_event(self, event_type: str | None, data: str) -> None:
        """Process a received SSE event.

        Args:
            event_type: Type of event (e.g., "security", "device")
            data: JSON data string
        """
        try:
            event_data = json.loads(data)
            _LOGGER.debug("SSE event received: type=%s, raw=%s", event_type, data[:500])

            # Add event type to data if not present
            if event_type and "eventType" not in event_data:
                event_data["eventType"] = event_type

            # Call callback
            if self._hass_loop:
                # Thread-safe callback to HA event loop — keep a strong ref so
                # the task is not garbage-collected before it completes.
                def _spawn() -> None:
                    task = asyncio.create_task(self._async_callback(event_data))
                    self._pending_callback_tasks.add(task)
                    task.add_done_callback(self._pending_callback_tasks.discard)

                self._hass_loop.call_soon_threadsafe(_spawn)
            else:
                result = self._callback(event_data)
                if asyncio.iscoroutine(result):
                    await result

        except json.JSONDecodeError:
            _LOGGER.error("Invalid JSON in SSE event: %s", data[:100])
        except Exception as err:
            _LOGGER.error("Error processing SSE event: %s", err)

    async def _async_callback(self, event_data: dict[str, Any]) -> None:
        """Async wrapper for callback."""
        try:
            result = self._callback(event_data)
            # If callback returns a coroutine, await it
            if asyncio.iscoroutine(result):
                await result
        except Exception as err:
            _LOGGER.error("SSE callback error: %s", err)

    def update_session_token(self, new_token: str) -> None:
        """Update session token (e.g., after token refresh).

        Args:
            new_token: New session token
        """
        self.session_token = new_token
        _LOGGER.debug("SSE session token updated")

    @property
    def is_connected(self) -> bool:
        """Check if SSE is currently connected."""
        return self._running and self._task is not None and not self._task.done()
