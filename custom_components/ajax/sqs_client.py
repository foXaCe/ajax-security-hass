"""AWS SQS client for Ajax - Rewritten clean version."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

try:
    from aiobotocore.session import get_session
    from botocore.exceptions import ClientError

    HAS_AIOBOTOCORE = True
except ImportError:
    HAS_AIOBOTOCORE = False
    get_session = None

_LOGGER = logging.getLogger(__name__)


class AjaxSQSClient:
    """Simple, robust SQS client for Ajax events."""

    # AWS Configuration
    REGION = "eu-west-1"
    WAIT_TIME = 5  # Shorter polling for faster response to rapid events
    MAX_MESSAGES = 10
    VISIBILITY_TIMEOUT = 30

    def __init__(
        self,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        queue_name: str,
        event_callback: Callable[[dict], Any] | None = None,
        hass_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the SQS client."""
        if not HAS_AIOBOTOCORE:
            raise ImportError("aiobotocore required")

        self._access_key = aws_access_key_id
        self._secret_key = aws_secret_access_key
        self._queue_name = queue_name
        self._callback = event_callback
        self._hass_loop = hass_loop

        self._session = get_session()
        self._queue_url: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def event_callback(self):
        return self._callback

    @event_callback.setter
    def event_callback(self, value):
        self._callback = value

    def _make_client(self):
        """Create a new SQS client context manager."""
        return self._session.create_client(
            "sqs",
            region_name=self.REGION,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        )

    async def connect(self) -> bool:
        """Connect to SQS and get queue URL."""
        try:
            # Run in executor to avoid blocking
            loop = asyncio.get_running_loop()
            self._queue_url = await loop.run_in_executor(None, self._get_queue_url_sync)
            _LOGGER.info("Connected to SQS: %s", self._queue_name)
            return True
        except Exception as err:
            _LOGGER.error("SQS connect failed: %s", err)
            return False

    def _get_queue_url_sync(self) -> str:
        """Synchronously get queue URL (runs in executor)."""

        async def _fetch():
            async with self._make_client() as client:
                resp = await client.get_queue_url(QueueName=self._queue_name)
                return resp["QueueUrl"]

        return asyncio.run(_fetch())

    async def start_receiving(self) -> None:
        """Start the background receive thread."""
        if self._thread and self._thread.is_alive():
            return
        if not self._queue_url:
            _LOGGER.error("Cannot start: not connected")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._receive_loop, name="SQS-Receiver", daemon=True)
        self._thread.start()
        _LOGGER.info("SQS receiver started")

    async def stop_receiving(self) -> None:
        """Stop the background receive thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=25)  # Wait for current poll to finish
        self._thread = None
        _LOGGER.info("SQS receiver stopped")

    async def close(self) -> None:
        """Close the client."""
        await self.stop_receiving()
        self._queue_url = None

    def _receive_loop(self) -> None:
        """Main receive loop (runs in dedicated thread)."""
        _LOGGER.info("SQS thread started")

        # Create event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        poll_count = 0
        consecutive_errors = 0
        try:
            while not self._stop_event.is_set():
                try:
                    poll_count += 1
                    _LOGGER.debug("SQS poll #%d starting...", poll_count)
                    messages = loop.run_until_complete(self._poll_messages())
                    consecutive_errors = 0  # Reset on success
                    _LOGGER.debug("SQS poll #%d returned %d messages", poll_count, len(messages))
                    for msg in messages:
                        loop.run_until_complete(self._handle_message(msg))

                    # If we got messages, immediately poll again to catch rapid events
                    # (FIFO queues may have more messages in the same group)
                    if messages:
                        _LOGGER.debug("Got messages, doing immediate follow-up poll")
                        continue  # Skip any wait, poll immediately

                except Exception as err:
                    _LOGGER.error("SQS poll error: %s", err, exc_info=True)
                    # Exponential backoff: 5s → 10s → 20s → 30s max
                    backoff = min(5 * (2**consecutive_errors), 30)
                    consecutive_errors += 1
                    # Wait with stop_event so we can exit quickly on shutdown
                    self._stop_event.wait(timeout=backoff)
        finally:
            loop.close()
            _LOGGER.info("SQS thread ended after %d polls", poll_count)

    async def _poll_messages(self) -> list[dict]:
        """Poll SQS for messages."""
        try:
            async with self._make_client() as client:
                response = await client.receive_message(
                    QueueUrl=self._queue_url,
                    MaxNumberOfMessages=self.MAX_MESSAGES,
                    WaitTimeSeconds=self.WAIT_TIME,
                    VisibilityTimeout=self.VISIBILITY_TIMEOUT,
                )
                return response.get("Messages", [])
        except ClientError as err:
            _LOGGER.error("SQS error: %s", err.response["Error"]["Code"])
            raise

    async def _handle_message(self, message: dict) -> None:
        """Process a single SQS message."""
        receipt = message.get("ReceiptHandle")
        msg_id = message.get("MessageId", "")[:8]

        try:
            # Parse the message
            body = json.loads(message.get("Body", "{}"))

            # Unwrap SNS envelope if present
            if "Message" in body and isinstance(body["Message"], str):
                body = json.loads(body["Message"])

            # Extract event info for logging
            event = body.get("event", {})
            event_tag = event.get("eventTag", "?")
            hub_id = event.get("hubId", "?")
            timestamp = event.get("timestamp", 0)

            if isinstance(timestamp, (int, float)) and time.time() - timestamp/1000 > 300:
                async with self._make_client() as client:
                    await client.delete_message(
                        QueueUrl=self._queue_url,
                        ReceiptHandle=receipt,
                    )
                    _LOGGER.info("SQS: deleted message because too old %s", msg_id)
                return

            _LOGGER.info("SQS: %s from hub %s (msg=%s)", event_tag, hub_id, msg_id)

            # Call the callback in Home Assistant's event loop
            if self._callback and self._hass_loop:
                future = asyncio.run_coroutine_threadsafe(self._callback(body), self._hass_loop)
                # Wait for callback to complete (with timeout)
                try:
                    if not future.result(timeout=15):
                        return
                except Exception as err:
                    _LOGGER.error("Callback error: %s", err)

            # Delete message from queue
            async with self._make_client() as client:
                await client.delete_message(
                    QueueUrl=self._queue_url,
                    ReceiptHandle=receipt,
                )
                _LOGGER.debug("SQS: deleted message %s", msg_id)

        except json.JSONDecodeError:
            _LOGGER.error("Invalid JSON in message %s", msg_id)
            # Still try to delete bad messages
            try:
                async with self._make_client() as client:
                    await client.delete_message(
                        QueueUrl=self._queue_url,
                        ReceiptHandle=receipt,
                    )
            except Exception:
                pass
        except Exception as err:
            _LOGGER.error("Message %s failed: %s", msg_id, err)
            # Still try to delete failed messages to unblock FIFO queue
            try:
                async with self._make_client() as client:
                    await client.delete_message(
                        QueueUrl=self._queue_url,
                        ReceiptHandle=receipt,
                    )
                    _LOGGER.debug("SQS: deleted failed message %s", msg_id)
            except Exception as del_err:
                _LOGGER.error("SQS: failed to delete message %s: %s", msg_id, del_err)
