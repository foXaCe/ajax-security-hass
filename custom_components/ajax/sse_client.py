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
from collections.abc import Callable
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


class AjaxSSEClient:
    """SSE client for Ajax real-time events via proxy."""

    # Reconnection settings
    RECONNECT_DELAY = 5  # seconds between reconnection attempts
    MAX_RECONNECT_DELAY = 60  # max delay between attempts
    CONNECTION_TIMEOUT = 30  # timeout for initial connection

    def __init__(
        self,
        sse_url: str,
        session_token: str,
        callback: Callable[[dict[str, Any]], None],
        hass_loop: asyncio.AbstractEventLoop | None = None,
    ):
        """Initialize the SSE client.

        Args:
            sse_url: URL of the SSE endpoint (from proxy login response)
            session_token: Session token for authentication
            callback: Function to call when an event is received
            hass_loop: Home Assistant event loop for thread-safe callbacks
        """
        self.sse_url = sse_url
        self.session_token = session_token
        self._callback = callback
        self._hass_loop = hass_loop
        self._running = False
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._reconnect_delay = self.RECONNECT_DELAY

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
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self.MAX_RECONNECT_DELAY
                )

    async def _connect_and_receive(self) -> None:
        """Connect to SSE endpoint and receive events."""
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()

        headers = {
            "X-Session-Token": self.session_token,
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }

        _LOGGER.debug("Connecting to SSE: %s", self.sse_url)

        async with self._session.get(
            self.sse_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=None,  # No total timeout for SSE
                connect=self.CONNECTION_TIMEOUT,
            ),
        ) as response:
            if response.status != 200:
                _LOGGER.error("SSE connection failed: HTTP %d", response.status)
                return

            _LOGGER.info("SSE connected successfully")
            self._reconnect_delay = self.RECONNECT_DELAY  # Reset on successful connect

            # Read SSE stream
            event_type = None
            event_data = []

            async for line in response.content:
                if not self._running:
                    break

                line = line.decode("utf-8").rstrip("\n\r")

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
            _LOGGER.debug("SSE event received: type=%s", event_type)

            # Add event type to data if not present
            if event_type and "eventType" not in event_data:
                event_data["eventType"] = event_type

            # Call callback
            if self._hass_loop:
                # Thread-safe callback to HA event loop
                self._hass_loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._async_callback(event_data))
                )
            else:
                self._callback(event_data)

        except json.JSONDecodeError:
            _LOGGER.error("Invalid JSON in SSE event: %s", data[:100])
        except Exception as err:
            _LOGGER.error("Error processing SSE event: %s", err)

    async def _async_callback(self, event_data: dict[str, Any]) -> None:
        """Async wrapper for callback."""
        try:
            self._callback(event_data)
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
