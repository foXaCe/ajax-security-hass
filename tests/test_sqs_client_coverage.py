"""Coverage tests for the SQS client's polling, message handling, and run loop.

These are light unit tests: the client is built via ``object.__new__`` to bypass
``__init__`` (which requires aiobotocore), and the AWS client / callbacks are
mocked. ``asyncio.sleep`` and ``_stop_event.wait`` are patched where needed so
backoffs are instantaneous and the run loop is driven deterministically.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ajax import sqs_client
from custom_components.ajax.sqs_client import AjaxSQSClient


def _make_bare_client(**overrides: Any) -> AjaxSQSClient:
    """Build an AjaxSQSClient without running __init__ (no aiobotocore needed)."""
    client = object.__new__(AjaxSQSClient)
    client._access_key = "key"
    client._secret_key = "secret"
    client._queue_name = "queue"
    client._callback = None
    client._hass_loop = None
    client._session = MagicMock()
    client._queue_url = "https://sqs.example/queue"
    client._stop_event = threading.Event()
    client._thread = None
    for name, value in overrides.items():
        setattr(client, name, value)
    return client


class _AsyncCMClient:
    """Async context manager wrapping a mock SQS client (mirrors aiobotocore)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __aenter__(self) -> Any:
        return self.inner

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _client_error(code: str) -> Exception:
    """Build a fake ClientError-like exception with a .response payload."""
    err = sqs_client.ClientError(f"boom {code}")
    err.response = {"Error": {"Code": code}}  # type: ignore[attr-defined]
    return err


@pytest.fixture
def distinct_client_error(monkeypatch: pytest.MonkeyPatch) -> type[Exception]:
    """Patch module ClientError to a class distinct from base Exception.

    In this test env aiobotocore is absent, so ``sqs_client.ClientError`` is
    ``Exception``; that collapses the ``except ClientError`` / ``except Exception``
    branches. Swapping in a dedicated subclass keeps the two paths separable.
    """

    class _FakeClientError(Exception):
        pass

    monkeypatch.setattr(sqs_client, "ClientError", _FakeClientError)
    return _FakeClientError


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make asyncio.sleep instantaneous so any backoff path is fast."""

    async def _noop(_delay: float, *_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)


# --------------------------------------------------------------------------- #
# _make_client
# --------------------------------------------------------------------------- #


def test_make_client_calls_session_create_client() -> None:
    client = _make_bare_client()
    sentinel = object()
    client._session.create_client.return_value = sentinel

    result = client._make_client()

    assert result is sentinel
    client._session.create_client.assert_called_once_with(
        "sqs",
        region_name=AjaxSQSClient.REGION,
        aws_access_key_id="key",
        aws_secret_access_key="secret",
    )


# --------------------------------------------------------------------------- #
# _poll_messages
# --------------------------------------------------------------------------- #


async def test_poll_messages_returns_messages() -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.receive_message = AsyncMock(return_value={"Messages": [{"MessageId": "1"}]})

    messages = await client._poll_messages(aws)

    assert messages == [{"MessageId": "1"}]
    aws.receive_message.assert_awaited_once_with(
        QueueUrl=client._queue_url,
        MaxNumberOfMessages=AjaxSQSClient.MAX_MESSAGES,
        WaitTimeSeconds=AjaxSQSClient.WAIT_TIME,
        VisibilityTimeout=AjaxSQSClient.VISIBILITY_TIMEOUT,
    )


async def test_poll_messages_defaults_to_empty_list() -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.receive_message = AsyncMock(return_value={})

    assert await client._poll_messages(aws) == []


# --------------------------------------------------------------------------- #
# _delete / _requeue
# --------------------------------------------------------------------------- #


async def test_delete_success() -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.delete_message = AsyncMock()

    await client._delete(aws, "receipt", "msgid")

    aws.delete_message.assert_awaited_once_with(QueueUrl=client._queue_url, ReceiptHandle="receipt")


async def test_delete_logs_on_failure() -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.delete_message = AsyncMock(side_effect=RuntimeError("nope"))

    # Should swallow the error.
    await client._delete(aws, "receipt", "msgid")


async def test_requeue_success() -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.change_message_visibility = AsyncMock()

    await client._requeue(aws, "receipt", "msgid")

    aws.change_message_visibility.assert_awaited_once_with(
        QueueUrl=client._queue_url,
        ReceiptHandle="receipt",
        VisibilityTimeout=0,
    )


async def test_requeue_logs_on_failure() -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.change_message_visibility = AsyncMock(side_effect=RuntimeError("nope"))

    await client._requeue(aws, "receipt", "msgid")


# --------------------------------------------------------------------------- #
# _handle_message
# --------------------------------------------------------------------------- #


async def test_handle_message_missing_receipt_skips() -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.delete_message = AsyncMock()

    await client._handle_message(aws, {"MessageId": "abcdef12", "Body": "{}"})

    aws.delete_message.assert_not_awaited()


async def test_handle_message_no_callback_deletes() -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.delete_message = AsyncMock()
    body = {"event": {"eventTag": "TEST", "hubId": "hub", "timestamp": time.time() * 1000}}
    message = {"ReceiptHandle": "r", "MessageId": "abcdef12", "Body": json.dumps(body)}

    await client._handle_message(aws, message)

    aws.delete_message.assert_awaited_once()


async def test_handle_message_unwraps_sns_envelope() -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.delete_message = AsyncMock()
    inner = {"event": {"eventTag": "TAG", "hubId": "h", "timestamp": time.time() * 1000}}
    envelope = {"Message": json.dumps(inner)}
    message = {"ReceiptHandle": "r", "MessageId": "abcdef12", "Body": json.dumps(envelope)}

    await client._handle_message(aws, message)

    aws.delete_message.assert_awaited_once()


async def test_handle_message_drops_stale_message() -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.delete_message = AsyncMock()
    # timestamp older than 300s (in ms): now - 600s, in ms.
    old_ts_ms = (time.time() - 600) * 1000
    body = {"event": {"eventTag": "OLD", "hubId": "h", "timestamp": old_ts_ms}}
    message = {"ReceiptHandle": "r", "MessageId": "abcdef12", "Body": json.dumps(body)}

    await client._handle_message(aws, message)

    # Stale path deletes then returns early.
    aws.delete_message.assert_awaited_once()


async def test_handle_message_callback_success_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.delete_message = AsyncMock()

    async def _cb(_body: dict[str, Any]) -> bool:
        return True

    client._callback = _cb
    client._hass_loop = MagicMock()

    fake_future = MagicMock()
    fake_future.result.return_value = True
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda coro, loop: (coro.close(), fake_future)[1])

    body = {"event": {"eventTag": "T", "hubId": "h", "timestamp": time.time() * 1000}}
    message = {"ReceiptHandle": "r", "MessageId": "abcdef12", "Body": json.dumps(body)}

    await client._handle_message(aws, message)

    aws.delete_message.assert_awaited_once()


async def test_handle_message_callback_false_requeues(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.delete_message = AsyncMock()
    aws.change_message_visibility = AsyncMock()

    async def _cb(_body: dict[str, Any]) -> bool:
        return False

    client._callback = _cb
    client._hass_loop = MagicMock()

    fake_future = MagicMock()
    fake_future.result.return_value = False
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda coro, loop: (coro.close(), fake_future)[1])

    body = {"event": {"eventTag": "T", "hubId": "h", "timestamp": time.time() * 1000}}
    message = {"ReceiptHandle": "r", "MessageId": "abcdef12", "Body": json.dumps(body)}

    await client._handle_message(aws, message)

    aws.change_message_visibility.assert_awaited_once()
    aws.delete_message.assert_not_awaited()


async def test_handle_message_callback_raises_requeues(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.delete_message = AsyncMock()
    aws.change_message_visibility = AsyncMock()

    async def _cb(_body: dict[str, Any]) -> bool:
        return True

    client._callback = _cb
    client._hass_loop = MagicMock()

    fake_future = MagicMock()
    fake_future.result.side_effect = TimeoutError("callback too slow")
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda coro, loop: (coro.close(), fake_future)[1])

    body = {"event": {"eventTag": "T", "hubId": "h", "timestamp": time.time() * 1000}}
    message = {"ReceiptHandle": "r", "MessageId": "abcdef12", "Body": json.dumps(body)}

    await client._handle_message(aws, message)

    aws.change_message_visibility.assert_awaited_once()
    aws.delete_message.assert_not_awaited()


async def test_handle_message_invalid_json_deletes() -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.delete_message = AsyncMock()
    message = {"ReceiptHandle": "r", "MessageId": "abcdef12", "Body": "not json{"}

    await client._handle_message(aws, message)

    aws.delete_message.assert_awaited_once()


async def test_handle_message_generic_error_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_bare_client()
    aws = MagicMock()
    aws.delete_message = AsyncMock()

    # Force an unexpected error after JSON parse by making time.time raise.
    monkeypatch.setattr(sqs_client.time, "time", MagicMock(side_effect=RuntimeError("boom")))

    body = {"event": {"eventTag": "T", "hubId": "h", "timestamp": 123}}
    message = {"ReceiptHandle": "r", "MessageId": "abcdef12", "Body": json.dumps(body)}

    await client._handle_message(aws, message)

    aws.delete_message.assert_awaited_once()


# --------------------------------------------------------------------------- #
# _run_loop_async
# --------------------------------------------------------------------------- #


async def test_run_loop_async_processes_messages_then_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_bare_client()
    inner = MagicMock()
    client._make_client = lambda: _AsyncCMClient(inner)

    calls = {"poll": 0, "handle": 0}

    async def _poll(_c: Any) -> list[dict[str, Any]]:
        calls["poll"] += 1
        if calls["poll"] == 1:
            return [{"MessageId": "1"}, {"MessageId": "2"}]
        # After processing the batch, stop the loop.
        client._stop_event.set()
        return []

    async def _handle(_c: Any, _m: dict[str, Any]) -> None:
        calls["handle"] += 1

    client._poll_messages = _poll
    client._handle_message = _handle

    await client._run_loop_async()

    # Two messages handled; the "if messages: continue" path triggers re-poll.
    assert calls["handle"] == 2
    assert calls["poll"] >= 2


async def test_run_loop_async_stop_event_breaks_message_loop() -> None:
    client = _make_bare_client()
    inner = MagicMock()
    client._make_client = lambda: _AsyncCMClient(inner)

    handled: list[str] = []

    async def _poll(_c: Any) -> list[dict[str, Any]]:
        if not handled:
            return [{"MessageId": "a"}, {"MessageId": "b"}]
        return []

    async def _handle(_c: Any, msg: dict[str, Any]) -> None:
        handled.append(msg["MessageId"])
        # Set stop after the first message so the inner loop breaks before "b".
        client._stop_event.set()

    client._poll_messages = _poll
    client._handle_message = _handle

    await client._run_loop_async()

    assert handled == ["a"]


async def test_run_loop_async_fatal_client_error_stops(distinct_client_error: type[Exception]) -> None:
    client = _make_bare_client()
    inner = MagicMock()
    client._make_client = lambda: _AsyncCMClient(inner)

    async def _poll(_c: Any) -> list[dict[str, Any]]:
        raise _client_error("AccessDenied")

    client._poll_messages = _poll

    await client._run_loop_async()

    assert client._stop_event.is_set()


async def test_run_loop_async_nonfatal_client_error_backs_off(
    distinct_client_error: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_bare_client()
    inner = MagicMock()
    client._make_client = lambda: _AsyncCMClient(inner)

    poll_calls = {"n": 0}

    async def _poll(_c: Any) -> list[dict[str, Any]]:
        poll_calls["n"] += 1
        if poll_calls["n"] == 1:
            raise _client_error("ServiceUnavailable")  # non-fatal
        client._stop_event.set()
        return []

    client._poll_messages = _poll

    waits: list[Any] = []

    def _fake_wait(timeout: Any) -> bool:
        waits.append(timeout)
        return True

    # _stop_event.wait is invoked through run_in_executor; replace it.
    monkeypatch.setattr(client._stop_event, "wait", _fake_wait)

    await client._run_loop_async()

    # Backoff path called _stop_event.wait once with the computed backoff (5s).
    assert waits == [5]
    assert client._stop_event.is_set()


async def test_run_loop_async_generic_exception_backs_off(
    distinct_client_error: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_bare_client()
    inner = MagicMock()
    client._make_client = lambda: _AsyncCMClient(inner)

    poll_calls = {"n": 0}

    async def _poll(_c: Any) -> list[dict[str, Any]]:
        poll_calls["n"] += 1
        if poll_calls["n"] == 1:
            raise RuntimeError("transient")
        client._stop_event.set()
        return []

    client._poll_messages = _poll

    waits: list[Any] = []

    def _fake_wait(timeout: Any) -> bool:
        waits.append(timeout)
        return True

    monkeypatch.setattr(client._stop_event, "wait", _fake_wait)

    await client._run_loop_async()

    assert waits == [5]
    assert client._stop_event.is_set()


async def test_run_loop_async_exits_immediately_when_stopped() -> None:
    client = _make_bare_client()
    inner = MagicMock()
    client._make_client = lambda: _AsyncCMClient(inner)
    client._stop_event.set()

    polled = {"n": 0}

    async def _poll(_c: Any) -> list[dict[str, Any]]:
        polled["n"] += 1
        return []

    client._poll_messages = _poll

    await client._run_loop_async()

    assert polled["n"] == 0


# --------------------------------------------------------------------------- #
# start_receiving / stop_receiving / close
# --------------------------------------------------------------------------- #


async def test_start_receiving_no_queue_url_logs_and_returns() -> None:
    client = _make_bare_client(_queue_url=None)
    await client.start_receiving()
    assert client._thread is None


async def test_start_receiving_already_alive_returns() -> None:
    client = _make_bare_client()
    alive_thread = MagicMock()
    alive_thread.is_alive.return_value = True
    client._thread = alive_thread

    await client.start_receiving()

    # Existing thread kept, no new thread started.
    assert client._thread is alive_thread


async def test_start_receiving_spawns_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_bare_client()
    client._receive_loop = lambda: None

    created: list[Any] = []
    real_thread = threading.Thread

    def _capture(*args: Any, **kwargs: Any) -> Any:
        t = real_thread(*args, **kwargs)
        created.append(t)
        return t

    monkeypatch.setattr(sqs_client.threading, "Thread", _capture)

    await client.start_receiving()

    assert client._thread is not None
    assert created
    client._thread.join(timeout=5)


async def test_stop_receiving_joins_live_thread() -> None:
    client = _make_bare_client()
    thread = MagicMock()
    thread.is_alive.return_value = True
    client._thread = thread

    await client.stop_receiving()

    thread.join.assert_called_once_with(timeout=5)
    assert client._thread is None
    assert client._stop_event.is_set()


async def test_stop_receiving_no_thread() -> None:
    client = _make_bare_client(_thread=None)
    await client.stop_receiving()
    assert client._thread is None


async def test_close_clears_queue_url() -> None:
    client = _make_bare_client()
    client._thread = None
    await client.close()
    assert client._queue_url is None
    assert client._stop_event.is_set()


# --------------------------------------------------------------------------- #
# event_callback property
# --------------------------------------------------------------------------- #


def test_event_callback_property_roundtrip() -> None:
    client = _make_bare_client()

    def _cb(_body: dict[str, Any]) -> Any:
        return None

    assert client.event_callback is None
    client.event_callback = _cb
    assert client.event_callback is _cb
    assert client._callback is _cb


# --------------------------------------------------------------------------- #
# connect / _get_queue_url_sync
# --------------------------------------------------------------------------- #


async def test_connect_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_bare_client(_queue_url=None)

    async def _run_in_executor(_executor: Any, func: Any, *args: Any) -> Any:
        return "https://sqs.example/resolved"

    loop = MagicMock()
    loop.run_in_executor = _run_in_executor
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)

    assert await client.connect() is True
    assert client._queue_url == "https://sqs.example/resolved"


async def test_connect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_bare_client(_queue_url=None)

    async def _run_in_executor(_executor: Any, func: Any, *args: Any) -> Any:
        raise RuntimeError("dns down")

    loop = MagicMock()
    loop.run_in_executor = _run_in_executor
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)

    assert await client.connect() is False


def test_get_queue_url_sync() -> None:
    """The sync helper drives a fresh loop via asyncio.run to resolve the URL.

    This test is synchronous (no running loop), so the inner ``asyncio.run`` in
    ``_get_queue_url_sync`` works without conflicting with pytest-asyncio.
    """
    client = _make_bare_client()
    inner = MagicMock()
    inner.get_queue_url = AsyncMock(return_value={"QueueUrl": "https://sqs.example/q"})
    client._make_client = lambda: _AsyncCMClient(inner)

    result = client._get_queue_url_sync()

    assert result == "https://sqs.example/q"
    inner.get_queue_url.assert_awaited_once_with(QueueName="queue")
