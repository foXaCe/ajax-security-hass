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
            self._thread.join(timeout=5)  # Wait for current poll to finish
        self._thread = None
        _LOGGER.info("SQS receiver stopped")

    async def close(self) -> None:
        """Close the client."""
        await self.stop_receiving()
        self._queue_url = None

    # Callback timeout must stay well below VISIBILITY_TIMEOUT so redelivered
    # messages never overlap with an in-flight callback.
    CALLBACK_TIMEOUT = 10

    # Non-recoverable SQS errors that should stop the loop instead of retrying.
    _FATAL_CLIENT_ERRORS = frozenset(
        {
            "InvalidClientTokenId",
            "SignatureDoesNotMatch",
            "AccessDenied",
            "UnrecognizedClientException",
            "AWS.SimpleQueueService.NonExistentQueue",
        }
    )

    def _receive_loop(self) -> None:
        """Main receive loop (runs in dedicated thread).

        Reuses a single aiobotocore client for the lifetime of the thread,
        avoiding per-message TCP/TLS handshakes and socket leaks.
        """
        _LOGGER.info("SQS thread started")

        # Create event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        poll_count = 0
        try:
            loop.run_until_complete(self._run_loop_async(lambda: poll_count))
        finally:
            try:
                # Cancel pending tasks before closing the loop.
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            finally:
                loop.close()
                _LOGGER.info("SQS thread ended")

    async def _run_loop_async(self, _poll_count_fn) -> None:
        """Run the poll/handle cycle with a persistent SQS client."""
        consecutive_errors = 0
        async with self._make_client() as client:
            while not self._stop_event.is_set():
                try:
                    messages = await self._poll_messages(client)
                    consecutive_errors = 0  # Reset on success
                    for msg in messages:
                        if self._stop_event.is_set():
                            break
                        await self._handle_message(client, msg)
                    if messages:
                        continue  # Poll immediately for rapid FIFO bursts
                except ClientError as err:
                    code = err.response.get("Error", {}).get("Code", "")
                    if code in self._FATAL_CLIENT_ERRORS:
                        _LOGGER.error("SQS fatal error %s — stopping receive loop", code)
                        self._stop_event.set()
                        break
                    _LOGGER.error("SQS poll error: %s", code or err)
                    backoff = min(5 * (2**consecutive_errors), 30)
                    consecutive_errors += 1
                    await asyncio.get_running_loop().run_in_executor(None, self._stop_event.wait, backoff)
                except Exception as err:
                    _LOGGER.error("SQS poll error: %s", err)
                    backoff = min(5 * (2**consecutive_errors), 30)
                    consecutive_errors += 1
                    await asyncio.get_running_loop().run_in_executor(None, self._stop_event.wait, backoff)

    async def _poll_messages(self, client) -> list[dict]:
        """Poll SQS for messages using an existing client."""
        response = await client.receive_message(
            QueueUrl=self._queue_url,
            MaxNumberOfMessages=self.MAX_MESSAGES,
            WaitTimeSeconds=self.WAIT_TIME,
            VisibilityTimeout=self.VISIBILITY_TIMEOUT,
        )
        return response.get("Messages", [])

    async def _delete(self, client, receipt: str, msg_id: str) -> None:
        """Delete a message, logging failures."""
        try:
            await client.delete_message(QueueUrl=self._queue_url, ReceiptHandle=receipt)
            _LOGGER.debug("SQS: deleted message %s", msg_id)
        except Exception as del_err:  # noqa: BLE001
            _LOGGER.error("SQS: failed to delete message %s: %s", msg_id, del_err)

    async def _handle_message(self, client, message: dict) -> None:
        """Process a single SQS message using the shared client."""
        receipt = message.get("ReceiptHandle")
        msg_id = message.get("MessageId", "")[:8]

        try:
            body = json.loads(message.get("Body", "{}"))

            # Unwrap SNS envelope if present
            if "Message" in body and isinstance(body["Message"], str):
                body = json.loads(body["Message"])

            event = body.get("event", {})
            event_tag = event.get("eventTag", "?")
            hub_id = event.get("hubId", "?")
            timestamp = event.get("timestamp")

            if isinstance(timestamp, (int, float)) and time.time() - timestamp / 1000 > 300:
                await self._delete(client, receipt, msg_id)
                _LOGGER.debug("SQS: dropped stale message %s", msg_id)
                return

            _LOGGER.debug("SQS: %s from hub %s (msg=%s)", event_tag, hub_id, msg_id)

            # Call the callback in Home Assistant's event loop
            if self._callback and self._hass_loop:
                future = asyncio.run_coroutine_threadsafe(self._callback(body), self._hass_loop)
                try:
                    if not future.result(timeout=self.CALLBACK_TIMEOUT):
                        try:
                            await client.change_message_visibility(
                                QueueUrl=self._queue_url,
                                ReceiptHandle=receipt,
                                VisibilityTimeout=0,
                            )
                            _LOGGER.debug("SQS: message made visible again %s", msg_id)
                        except Exception as requeue_err:
                            _LOGGER.error(
                                "SQS: failed to make message visible again %s: %s",
                                msg_id,
                                requeue_err,
                            )
                        return
                except Exception as err:
                    _LOGGER.error("Callback error: %s", err)

            await self._delete(client, receipt, msg_id)

        except json.JSONDecodeError:
            _LOGGER.error("Invalid JSON in message %s", msg_id)
            await self._delete(client, receipt, msg_id)
        except Exception as err:
            _LOGGER.error("Message %s failed: %s", msg_id, err)
            # Delete failed messages to unblock FIFO queue
            await self._delete(client, receipt, msg_id)
