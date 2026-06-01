"""Unit coverage for the SSE client (sse_client.py).

These tests exercise the connection lifecycle, the SSE line-parser
(``event:`` / ``data:`` / comment / raw-JSON forms), callback dispatch for
both sync and coroutine callbacks, the reconnection backoff in the receive
loop, the AUTH_FAILURE_THRESHOLD reauth escalation on repeated 401/403, and
``stop()`` draining in-flight callback tasks.

aiohttp is faked at the ``ClientSession`` level: a fake session's ``get(...)``
returns an async context manager whose ``content`` async-iterates the raw SSE
byte lines and whose ``status`` is configurable. ``asyncio.sleep`` is patched
so backoff is instantaneous.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ajax.sse_client import AjaxSSEClient, _safe_url

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeContent:
    """Async-iterable stand-in for ``response.content`` (yields byte lines)."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            for line in self._lines:
                yield line

        return _gen()


class _FakeResponse:
    """Async context manager mimicking an aiohttp streaming response."""

    def __init__(self, status: int = 200, lines: list[bytes] | None = None) -> None:
        self.status = status
        self.content = _FakeContent(lines or [])

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Fake aiohttp.ClientSession whose ``get`` returns a queued response."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.closed = False
        self.get_calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.get_calls.append({"url": url, **kwargs})
        return self._response

    async def close(self) -> None:
        self.closed = True


def _client(
    *,
    callback: Any = None,
    hass_loop: Any = None,
    user_id: str | None = None,
    verify_ssl: bool = True,
    token_provider: Any = None,
    on_auth_failure: Any = None,
) -> AjaxSSEClient:
    """Build a client without spawning the receive task."""
    return AjaxSSEClient(
        sse_url="https://proxy.example.com/sse?userId=secret",
        session_token="tok-initial",
        callback=callback or MagicMock(),
        hass_loop=hass_loop,
        user_id=user_id,
        verify_ssl=verify_ssl,
        token_provider=token_provider,
        on_auth_failure=on_auth_failure,
    )


@pytest.fixture(autouse=True)
def _no_sleep() -> Any:
    """Make all backoff sleeps instantaneous."""
    with patch("custom_components.ajax.sse_client.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        yield sleep_mock


# ---------------------------------------------------------------------------
# _safe_url
# ---------------------------------------------------------------------------


def test_safe_url_strips_query() -> None:
    assert _safe_url("https://host/sse?userId=secret&t=1") == "https://host/sse"


def test_safe_url_invalid() -> None:
    # urlsplit raises ValueError on a malformed bracketed host/port.
    assert _safe_url("http://[::1") == "<invalid-url>"


# ---------------------------------------------------------------------------
# start / stop / is_connected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_spawns_task_then_stop_cancels() -> None:
    client = _client()
    # Replace the receive loop with a long sleep so the task stays alive.
    client._receive_loop = AsyncMock(side_effect=lambda: asyncio.sleep(3600))  # type: ignore[method-assign]

    started = await client.start()
    assert started is True
    assert client._running is True
    assert client.is_connected is True

    await client.stop()
    assert client._running is False
    assert client._task is None
    assert client.is_connected is False


@pytest.mark.asyncio
async def test_start_when_already_running_returns_true_without_new_task() -> None:
    client = _client()
    client._running = True
    with patch.object(asyncio, "create_task") as create_task:
        result = await client.start()
    assert result is True
    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_stop_closes_session_and_drains_pending_tasks() -> None:
    client = _client()
    session = _FakeSession(_FakeResponse())
    client._session = session  # type: ignore[assignment]

    drained = asyncio.Event()

    async def _pending() -> None:
        drained.set()

    task = asyncio.ensure_future(_pending())
    client._pending_callback_tasks.add(task)

    await client.stop()

    assert drained.is_set()
    assert client._pending_callback_tasks == set()
    assert session.closed is True
    assert client._session is None


@pytest.mark.asyncio
async def test_stop_without_task_or_session_is_noop() -> None:
    client = _client()
    # No task, no session — must not raise.
    await client.stop()
    assert client._running is False


@pytest.mark.asyncio
async def test_is_connected_false_when_task_done() -> None:
    client = _client()

    async def _noop() -> None:
        return None

    client._running = True
    client._task = asyncio.ensure_future(_noop())
    await client._task
    assert client.is_connected is False


# ---------------------------------------------------------------------------
# _connect_and_receive: stream parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_parses_event_and_data_lines() -> None:
    received: list[dict[str, Any]] = []

    async def _cb(data: dict[str, Any]) -> None:
        received.append(data)

    client = _client(callback=_cb)
    client._running = True
    lines = [
        b"event: security\n",
        b'data: {"foo": "bar"}\n',
        b"\n",  # blank line flushes the event
    ]
    client._session = _FakeSession(_FakeResponse(200, lines))  # type: ignore[assignment]

    await client._connect_and_receive()

    assert received == [{"foo": "bar", "eventType": "security"}]
    # Reset backoff/auth counters on a successful connect.
    assert client._reconnect_delay == AjaxSSEClient.RECONNECT_DELAY
    assert client._auth_failures == 0


@pytest.mark.asyncio
async def test_connect_multiline_data_is_joined() -> None:
    received: list[dict[str, Any]] = []

    async def _cb(data: dict[str, Any]) -> None:
        received.append(data)

    client = _client(callback=_cb)
    client._running = True
    lines = [
        b'data: {"a":\n',
        b"data: 1}\n",
        b"\n",
    ]
    client._session = _FakeSession(_FakeResponse(200, lines))  # type: ignore[assignment]

    await client._connect_and_receive()
    assert received == [{"a": 1}]


@pytest.mark.asyncio
async def test_connect_raw_json_line() -> None:
    """Julien's proxy format: a bare JSON object on its own line."""
    received: list[dict[str, Any]] = []

    async def _cb(data: dict[str, Any]) -> None:
        received.append(data)

    client = _client(callback=_cb)
    client._running = True
    lines = [b'{"raw": true}\n']
    client._session = _FakeSession(_FakeResponse(200, lines))  # type: ignore[assignment]

    await client._connect_and_receive()
    assert received == [{"raw": True}]


@pytest.mark.asyncio
async def test_connect_keepalive_comment_is_ignored() -> None:
    cb = MagicMock()
    client = _client(callback=cb)
    client._running = True
    lines = [b": keepalive\n"]
    client._session = _FakeSession(_FakeResponse(200, lines))  # type: ignore[assignment]

    await client._connect_and_receive()
    cb.assert_not_called()


@pytest.mark.asyncio
async def test_connect_oversized_line_is_skipped() -> None:
    cb = MagicMock()
    client = _client(callback=cb)
    big = b"x" * (1 * 1024 * 1024 + 1)
    lines = [big, b'{"ok": 1}\n']
    client._session = _FakeSession(_FakeResponse(200, lines))  # type: ignore[assignment]
    client._running = True

    await client._connect_and_receive()
    # Oversized line skipped, JSON line still processed.
    cb.assert_called_once_with({"ok": 1})


@pytest.mark.asyncio
async def test_connect_stops_iterating_when_not_running() -> None:
    cb = MagicMock()
    client = _client(callback=cb)

    # First line flips _running off; subsequent lines must not be parsed.
    class _StoppingContent:
        def __aiter__(self) -> AsyncIterator[bytes]:
            async def _gen() -> AsyncIterator[bytes]:
                client._running = False
                yield b'{"never": 1}\n'

            return _gen()

    resp = _FakeResponse(200, [])
    resp.content = _StoppingContent()  # type: ignore[assignment]
    client._session = _FakeSession(resp)  # type: ignore[assignment]
    client._running = True

    await client._connect_and_receive()
    cb.assert_not_called()


# ---------------------------------------------------------------------------
# _connect_and_receive: HTTP failures / auth escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_non_200_escalates_backoff() -> None:
    client = _client()
    client._session = _FakeSession(_FakeResponse(500, []))  # type: ignore[assignment]
    before = client._reconnect_delay

    await client._connect_and_receive()

    assert client._reconnect_delay == before * 2
    assert client._auth_failures == 0
    assert client._running is False  # default; never started


@pytest.mark.asyncio
async def test_connect_401_increments_auth_failures_below_threshold() -> None:
    on_auth = MagicMock()
    client = _client(on_auth_failure=on_auth)
    client._running = True
    client._session = _FakeSession(_FakeResponse(401, []))  # type: ignore[assignment]

    await client._connect_and_receive()

    assert client._auth_failures == 1
    on_auth.assert_not_called()
    assert client._running is True


@pytest.mark.asyncio
async def test_connect_401_threshold_triggers_reauth_and_stops() -> None:
    on_auth = MagicMock()
    client = _client(on_auth_failure=on_auth)
    client._running = True
    client._auth_failures = AjaxSSEClient.AUTH_FAILURE_THRESHOLD - 1
    client._session = _FakeSession(_FakeResponse(403, []))  # type: ignore[assignment]

    await client._connect_and_receive()

    assert client._auth_failures == AjaxSSEClient.AUTH_FAILURE_THRESHOLD
    on_auth.assert_called_once()
    assert client._running is False


@pytest.mark.asyncio
async def test_connect_reauth_callback_exception_is_swallowed() -> None:
    on_auth = MagicMock(side_effect=RuntimeError("boom"))
    client = _client(on_auth_failure=on_auth)
    client._running = True
    client._auth_failures = AjaxSSEClient.AUTH_FAILURE_THRESHOLD - 1
    client._session = _FakeSession(_FakeResponse(401, []))  # type: ignore[assignment]

    # Must not propagate the callback error.
    await client._connect_and_receive()
    on_auth.assert_called_once()
    assert client._running is False


@pytest.mark.asyncio
async def test_connect_401_no_callback_does_not_crash() -> None:
    client = _client(on_auth_failure=None)
    client._running = True
    client._auth_failures = AjaxSSEClient.AUTH_FAILURE_THRESHOLD - 1
    client._session = _FakeSession(_FakeResponse(401, []))  # type: ignore[assignment]

    await client._connect_and_receive()
    # Threshold reached but no callback => running stays True (no escalation).
    assert client._auth_failures == AjaxSSEClient.AUTH_FAILURE_THRESHOLD
    assert client._running is True


# ---------------------------------------------------------------------------
# _connect_and_receive: session creation + headers + token_provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_creates_session_with_verify_ssl_true() -> None:
    created = _FakeSession(_FakeResponse(200, []))
    client = _client(verify_ssl=True, user_id="U42")

    with patch(
        "custom_components.ajax.sse_client.aiohttp.ClientSession",
        return_value=created,
    ) as mk:
        await client._connect_and_receive()

    # verify_ssl True => connector kwarg is None.
    mk.assert_called_once_with(connector=None)
    assert client._session is created
    headers = created.get_calls[0]["headers"]
    assert headers["X-Session-Token"] == "tok-initial"
    assert headers["Accept"] == "text/event-stream"
    assert headers["X-User-Id"] == "U42"


@pytest.mark.asyncio
async def test_connect_creates_session_with_verify_ssl_false() -> None:
    created = _FakeSession(_FakeResponse(200, []))
    client = _client(verify_ssl=False)

    fake_connector = object()
    with (
        patch(
            "custom_components.ajax.sse_client.aiohttp.ClientSession",
            return_value=created,
        ) as mk,
        patch(
            "custom_components.ajax.sse_client.aiohttp.TCPConnector",
            return_value=fake_connector,
        ) as conn,
    ):
        await client._connect_and_receive()

    conn.assert_called_once_with(ssl=False)
    mk.assert_called_once_with(connector=fake_connector)
    # No user_id => header absent.
    assert "X-User-Id" not in created.get_calls[0]["headers"]


@pytest.mark.asyncio
async def test_connect_uses_fresh_token_from_provider() -> None:
    client = _client(token_provider=lambda: "tok-fresh")
    created = _FakeSession(_FakeResponse(200, []))
    client._session = created  # type: ignore[assignment]

    await client._connect_and_receive()

    assert client.session_token == "tok-fresh"
    assert created.get_calls[0]["headers"]["X-Session-Token"] == "tok-fresh"


@pytest.mark.asyncio
async def test_connect_token_provider_returns_none_keeps_old_token() -> None:
    client = _client(token_provider=lambda: None)
    created = _FakeSession(_FakeResponse(200, []))
    client._session = created  # type: ignore[assignment]

    await client._connect_and_receive()

    assert client.session_token == "tok-initial"


@pytest.mark.asyncio
async def test_connect_reuses_open_session() -> None:
    existing = _FakeSession(_FakeResponse(200, []))
    client = _client()
    client._session = existing  # type: ignore[assignment]

    with patch("custom_components.ajax.sse_client.aiohttp.ClientSession") as mk:
        await client._connect_and_receive()

    mk.assert_not_called()
    assert client._session is existing


# ---------------------------------------------------------------------------
# _process_event: dispatch paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_event_sync_callback() -> None:
    cb = MagicMock()
    client = _client(callback=cb)
    await client._process_event("device", '{"x": 1}')
    cb.assert_called_once_with({"x": 1, "eventType": "device"})


@pytest.mark.asyncio
async def test_process_event_coroutine_callback_is_awaited() -> None:
    awaited: list[dict[str, Any]] = []

    async def _cb(data: dict[str, Any]) -> None:
        awaited.append(data)

    client = _client(callback=_cb)
    await client._process_event(None, '{"x": 2}')
    assert awaited == [{"x": 2}]


@pytest.mark.asyncio
async def test_process_event_keeps_existing_event_type() -> None:
    cb = MagicMock()
    client = _client(callback=cb)
    await client._process_event("security", '{"eventType": "override"}')
    cb.assert_called_once_with({"eventType": "override"})


@pytest.mark.asyncio
async def test_process_event_invalid_json_logged_not_raised() -> None:
    cb = MagicMock()
    client = _client(callback=cb)
    await client._process_event("device", "not json")
    cb.assert_not_called()


@pytest.mark.asyncio
async def test_process_event_callback_exception_in_sync_path_swallowed() -> None:
    cb = MagicMock(side_effect=RuntimeError("kaboom"))
    client = _client(callback=cb)
    # Exception in the sync callback is caught by the outer handler.
    await client._process_event(None, '{"a": 1}')
    cb.assert_called_once()


@pytest.mark.asyncio
async def test_process_event_hass_loop_spawns_strong_ref_task() -> None:
    spawned: list[Any] = []

    def _call_soon_threadsafe(fn: Any) -> None:
        # Run the spawner synchronously so we can assert task tracking.
        spawned.append(fn)
        fn()

    loop = MagicMock()
    loop.call_soon_threadsafe.side_effect = _call_soon_threadsafe

    done = asyncio.Event()

    async def _cb(data: dict[str, Any]) -> None:
        done.set()

    client = _client(callback=_cb, hass_loop=loop)
    await client._process_event("security", '{"y": 9}')

    # The spawner ran synchronously and kept a strong ref to the task (so it
    # cannot be GC'd before completing) — that is the point of this code path.
    assert loop.call_soon_threadsafe.called
    tracked = list(client._pending_callback_tasks)
    assert len(tracked) == 1

    # Awaiting the task runs its body (done.set()) and, once finished, fires the
    # add_done_callback(discard) that removes it from the tracking set.
    await asyncio.gather(*tracked)
    assert done.is_set()
    assert client._pending_callback_tasks == set()


# ---------------------------------------------------------------------------
# _async_callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_callback_awaits_coroutine() -> None:
    seen: list[dict[str, Any]] = []

    async def _cb(data: dict[str, Any]) -> None:
        seen.append(data)

    client = _client(callback=_cb)
    await client._async_callback({"k": "v"})
    assert seen == [{"k": "v"}]


@pytest.mark.asyncio
async def test_async_callback_sync_callback() -> None:
    cb = MagicMock()
    client = _client(callback=cb)
    await client._async_callback({"k": "v"})
    cb.assert_called_once_with({"k": "v"})


@pytest.mark.asyncio
async def test_async_callback_swallows_exception() -> None:
    cb = MagicMock(side_effect=ValueError("nope"))
    client = _client(callback=cb)
    # Must not raise.
    await client._async_callback({"k": "v"})
    cb.assert_called_once()


# ---------------------------------------------------------------------------
# _receive_loop: reconnection + backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_receive_loop_reconnects_with_backoff(_no_sleep: AsyncMock) -> None:
    client = _client()
    calls = {"n": 0}

    async def _connect() -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            client._running = False

    client._running = True
    client._connect_and_receive = _connect  # type: ignore[method-assign]

    await client._receive_loop()

    assert calls["n"] == 2
    # Slept once between the two attempts and bumped the backoff.
    _no_sleep.assert_awaited()
    assert client._reconnect_delay == AjaxSSEClient.RECONNECT_DELAY * 2


@pytest.mark.asyncio
async def test_receive_loop_logs_and_retries_on_exception(
    _no_sleep: AsyncMock,
) -> None:
    client = _client()
    calls = {"n": 0}

    async def _connect() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("network down")
        client._running = False

    client._running = True
    client._connect_and_receive = _connect  # type: ignore[method-assign]

    await client._receive_loop()
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_receive_loop_breaks_on_cancelled_error() -> None:
    client = _client()

    async def _connect() -> None:
        raise asyncio.CancelledError

    client._running = True
    client._connect_and_receive = _connect  # type: ignore[method-assign]

    # CancelledError breaks the loop without re-raising.
    await client._receive_loop()


@pytest.mark.asyncio
async def test_receive_loop_backoff_capped_at_max(_no_sleep: AsyncMock) -> None:
    client = _client()
    client._reconnect_delay = AjaxSSEClient.MAX_RECONNECT_DELAY
    calls = {"n": 0}

    async def _connect() -> None:
        calls["n"] += 1
        client._running = False

    client._running = True
    client._connect_and_receive = _connect  # type: ignore[method-assign]

    await client._receive_loop()
    assert client._reconnect_delay == AjaxSSEClient.MAX_RECONNECT_DELAY


# ---------------------------------------------------------------------------
# End-to-end: start drives the loop through one real parse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_to_event_end_to_end() -> None:
    received: list[dict[str, Any]] = []

    async def _cb(data: dict[str, Any]) -> None:
        received.append(data)
        # Stop after the first event so the loop terminates.
        client._running = False

    client = _client(callback=_cb)
    lines = [b'data: {"e": 1}\n', b"\n", b'data: {"e": 2}\n', b"\n"]
    client._session = _FakeSession(_FakeResponse(200, lines))  # type: ignore[assignment]

    await client.start()
    await asyncio.wait_for(client._task, timeout=2)  # type: ignore[arg-type]

    assert received == [{"e": 1}]
    assert json.dumps(received[0])  # serialisable sanity check
