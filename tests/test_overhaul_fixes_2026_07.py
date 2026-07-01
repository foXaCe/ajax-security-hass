"""Regression tests for the 2026-07 overhaul fixes.

Covers:
- shared tamper/device-status mutation helpers (transition-aware, single
  source of truth for SSE *and* SQS — the SQS path used to ignore
  ``transition`` and leave ``tampered`` stuck True);
- ONVIF reconcile (cameras added/removed after startup, bootstrap self-heal
  throttle);
- ``available`` gating on ``coordinator.last_update_success`` for the
  platforms that used to skip it (valve / lock / select);
- ``SIGNAL_NEW_SPACE`` dynamic discovery builders on the five platforms
  that used to ignore the signal (multi-hub added after startup);
- the quiet single-attempt SQS retry (``wait_for_dependency=False``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.ajax._coordinator_onvif import AjaxOnvifMixin
from custom_components.ajax._event_helpers import EventHandlerMixin
from custom_components.ajax.const import (
    SIGNAL_NEW_SPACE,
)
from custom_components.ajax.coordinator import AjaxDataCoordinator
from custom_components.ajax.models import (
    AjaxAccount,
    AjaxDevice,
    AjaxSmartLock,
    AjaxSpace,
    AjaxVideoEdge,
    DeviceType,
    VideoEdgeType,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _device(**kwargs: Any) -> AjaxDevice:
    defaults = {
        "id": "d1",
        "name": "Front Door",
        "type": DeviceType.DOOR_CONTACT,
        "space_id": "s1",
        "hub_id": "hub1",
    }
    defaults.update(kwargs)
    return AjaxDevice(**defaults)


def _space(**kwargs: Any) -> AjaxSpace:
    defaults = {"id": "s1", "name": "Home", "hub_id": "hub1"}
    defaults.update(kwargs)
    return AjaxSpace(**defaults)


def _account(space: AjaxSpace | None = None) -> AjaxAccount:
    account = AjaxAccount(user_id="u1", name="U", email="u@e.com")
    if space is not None:
        account.spaces[space.id] = space
    return account


# ---------------------------------------------------------------------------
# Shared tamper / device-status helpers (bug: SQS ignored ``transition``)
# ---------------------------------------------------------------------------


def test_apply_tamper_state_honours_transition() -> None:
    """``tamperopened`` is reused by Ajax for open AND close — the
    ``transition`` field must override the static tuple."""
    dev = _device()
    assert EventHandlerMixin._apply_tamper_state(dev, "tamperopened", "TRIGGERED") == "tamper_open"
    assert dev.attributes["tampered"] is True
    EventHandlerMixin._apply_tamper_state(dev, "tamperopened", "RECOVERED")
    assert dev.attributes["tampered"] is False


def test_apply_tamper_state_without_transition_uses_static_tuple() -> None:
    dev = _device()
    EventHandlerMixin._apply_tamper_state(dev, "lidopen", "")
    assert dev.attributes["tampered"] is True
    EventHandlerMixin._apply_tamper_state(dev, "lidclosed", "")
    assert dev.attributes["tampered"] is False


def test_apply_device_status_all_tags() -> None:
    dev = _device()
    EventHandlerMixin._apply_device_status(dev, "offline")
    assert dev.online is False
    EventHandlerMixin._apply_device_status(dev, "online")
    assert dev.online is True
    EventHandlerMixin._apply_device_status(dev, "lowbattery")
    assert dev.attributes["low_battery"] is True
    EventHandlerMixin._apply_device_status(dev, "batterycharged")
    assert dev.attributes["low_battery"] is False
    EventHandlerMixin._apply_device_status(dev, "externalpowerdisconnected")
    assert dev.attributes["externally_powered"] is False
    assert dev.is_optimistic("externally_powered") is True
    EventHandlerMixin._apply_device_status(dev, "externalpowerrestored")
    assert dev.attributes["externally_powered"] is True


async def test_sqs_tamper_recovered_clears_state() -> None:
    """THE bug: in direct/SQS mode a closed lid stayed ``tampered=True``."""
    from custom_components.ajax.sqs_manager import SQSManager

    mgr = object.__new__(SQSManager)
    mgr.coordinator = MagicMock()
    mgr._language = "en"
    space = _space()
    dev = _device()
    dev.attributes["tampered"] = True
    space.devices["d1"] = dev

    handled = await mgr._handle_device_status_event(space, "tamperopened", "Front Door", "d1", "RECOVERED")
    assert handled is True
    assert dev.attributes["tampered"] is False


# ---------------------------------------------------------------------------
# ONVIF reconcile (cameras added/removed after startup)
# ---------------------------------------------------------------------------


def _onvif_mixin() -> AjaxOnvifMixin:
    m = object.__new__(AjaxOnvifMixin)
    m._onvif_reconcile_in_progress = False
    m._onvif_last_bootstrap_attempt = 0.0
    m._onvif_initialized = True
    m.onvif_manager = None
    m.account = None
    return m


async def test_reconcile_onvif_updates_manager_with_current_edges() -> None:
    m = _onvif_mixin()
    m.onvif_manager = MagicMock()
    m.onvif_manager.async_update_video_edges = AsyncMock()
    cam = AjaxVideoEdge(id="cam1", name="Cam", space_id="s1", video_edge_type=VideoEdgeType.TURRET)
    cam.ip_address = "192.168.1.50"
    no_ip = AjaxVideoEdge(id="cam2", name="NoIP", space_id="s1", video_edge_type=VideoEdgeType.TURRET)
    space = _space()
    space.video_edges = {"cam1": cam, "cam2": no_ip}
    m.account = _account(space)

    await m._async_reconcile_onvif()

    m.onvif_manager.async_update_video_edges.assert_awaited_once()
    passed = m.onvif_manager.async_update_video_edges.await_args.args[0]
    # Only edges with an IP are handed to the manager.
    assert passed == [cam]
    assert m._onvif_reconcile_in_progress is False


async def test_reconcile_onvif_guard_prevents_overlap() -> None:
    m = _onvif_mixin()
    m.onvif_manager = MagicMock()
    m.onvif_manager.async_update_video_edges = AsyncMock()
    m.account = _account(_space())
    m._onvif_reconcile_in_progress = True

    await m._async_reconcile_onvif()

    m.onvif_manager.async_update_video_edges.assert_not_awaited()
    # The overlapping call must NOT clear the owner's flag.
    assert m._onvif_reconcile_in_progress is True


async def test_reconcile_onvif_bootstrap_retry_is_throttled() -> None:
    """No manager (init failed / no cameras at startup) -> re-bootstrap, but
    at most once per throttle window."""
    m = _onvif_mixin()
    m._async_init_onvif = AsyncMock()

    with patch("custom_components.ajax._coordinator_onvif.time.monotonic", side_effect=[1000.0, 1100.0, 1400.0]):
        await m._async_reconcile_onvif()  # t=1000: bootstraps
        await m._async_reconcile_onvif()  # t=1100: throttled (window 300s)
        await m._async_reconcile_onvif()  # t=1400: window elapsed, retries

    assert m._async_init_onvif.await_count == 2


# ---------------------------------------------------------------------------
# available — coordinator.last_update_success gate
# ---------------------------------------------------------------------------


def test_valve_unavailable_when_coordinator_failed() -> None:
    from custom_components.ajax.valve import AjaxValve

    space = _space()
    dev = _device(type=DeviceType.WATERSTOP, online=True)
    space.devices["d1"] = dev
    valve = object.__new__(AjaxValve)
    valve._space_id = "s1"
    valve._device_id = "d1"
    valve.coordinator = SimpleNamespace(last_update_success=False, get_space=lambda sid: space)
    assert valve.available is False
    valve.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: space)
    assert valve.available is True


def test_lock_unavailable_when_coordinator_failed() -> None:
    from custom_components.ajax.lock import AjaxLock

    space = _space()
    space.smart_locks["sl1"] = AjaxSmartLock(id="sl1", name="Doorman", space_id="s1")
    lock = object.__new__(AjaxLock)
    lock._space_id = "s1"
    lock._smart_lock_id = "sl1"
    lock.coordinator = SimpleNamespace(last_update_success=False, get_space=lambda sid: space)
    assert lock.available is False
    lock.coordinator = SimpleNamespace(last_update_success=True, get_space=lambda sid: space)
    assert lock.available is True


def test_handler_select_unavailable_when_coordinator_failed() -> None:
    from custom_components.ajax.select import AjaxHandlerSelect

    space = _space()
    dev = _device(type=DeviceType.SIREN, online=True)
    space.devices["d1"] = dev
    sel = object.__new__(AjaxHandlerSelect)
    sel._space_id = "s1"
    sel._device_id = "d1"
    sel.coordinator = SimpleNamespace(last_update_success=False, get_space=lambda sid: space)
    assert sel.available is False


# ---------------------------------------------------------------------------
# SIGNAL_NEW_SPACE dynamic discovery builders (multi-hub added after startup)
# ---------------------------------------------------------------------------


def _hub_space() -> AjaxSpace:
    space = _space(id="s9", hub_id="hub9")
    space.hub_details = {
        "firmware": {"version": "2.20.1"},
        "geoFence": {"latitude": 48.85, "longitude": 2.35},
        "battery": {"chargeLevelPercentage": 90},
    }
    return space


def _coordinator_double(space: AjaxSpace) -> MagicMock:
    coord = MagicMock()
    coord.entry_id = "entry_test"
    coord.last_update_success = True
    coord.account = _account(space)
    coord.data = coord.account
    coord.get_space = lambda sid: coord.account.spaces.get(sid)
    return coord


async def _capture_space_builder(module: Any, setup_name: str, coord: MagicMock) -> Any:
    """Run a platform's async_setup_entry and capture its SIGNAL_NEW_SPACE builder."""
    entry = SimpleNamespace(runtime_data=coord, entry_id="entry_test", async_on_unload=lambda _f: None)
    captured: dict[str, Any] = {}

    def _fake_connect(hass: Any, entry_: Any, signal: str, domain: str, add: Any, builder: Any, *, label: str) -> None:
        captured[signal] = builder

    with patch(f"custom_components.ajax.{module}.connect_new_entity_signal", _fake_connect):
        import importlib

        mod = importlib.import_module(f"custom_components.ajax.{module}")
        await getattr(mod, setup_name)(MagicMock(), entry, MagicMock())
    assert SIGNAL_NEW_SPACE in captured, f"{module} n'écoute pas SIGNAL_NEW_SPACE"
    return captured[SIGNAL_NEW_SPACE]


async def test_sensor_builds_space_and_hub_sensors_for_new_space() -> None:
    space = _hub_space()
    coord = _coordinator_double(space)
    builder = await _capture_space_builder("sensor", "async_setup_entry", coord)
    pairs = builder("s9", "s9")
    assert pairs, "aucune entité construite pour le nouveau hub"
    from custom_components.ajax.sensor import AjaxHubSensor, AjaxSpaceSensor

    kinds = {type(e) for _k, e in pairs}
    assert AjaxSpaceSensor in kinds
    assert AjaxHubSensor in kinds
    # Unknown space -> nothing.
    assert builder("ghost", "ghost") == []


async def test_binary_sensor_builds_hub_sensors_for_new_space() -> None:
    space = _hub_space()
    coord = _coordinator_double(space)
    builder = await _capture_space_builder("binary_sensor", "async_setup_entry", coord)
    pairs = builder("s9", "s9")
    from custom_components.ajax.binary_sensor import AjaxHubBinarySensor

    assert pairs
    assert {type(e) for _k, e in pairs} == {AjaxHubBinarySensor}
    assert builder("ghost", "ghost") == []


async def test_button_builds_panic_for_new_space() -> None:
    space = _hub_space()
    coord = _coordinator_double(space)
    builder = await _capture_space_builder("button", "async_setup_entry", coord)
    pairs = builder("s9", "s9")
    from custom_components.ajax.button import AjaxPanicButton

    assert len(pairs) == 1
    assert isinstance(pairs[0][1], AjaxPanicButton)
    assert builder("ghost", "ghost") == []


async def test_device_tracker_builds_tracker_for_new_space_with_geofence() -> None:
    space = _hub_space()
    coord = _coordinator_double(space)
    builder = await _capture_space_builder("device_tracker", "async_setup_entry", coord)
    pairs = builder("s9", "s9")
    from custom_components.ajax.device_tracker import AjaxHubTracker

    assert len(pairs) == 1
    assert isinstance(pairs[0][1], AjaxHubTracker)
    # No geofence -> no tracker.
    space.hub_details["geoFence"] = {}
    assert builder("s9", "s9") == []


async def test_update_builds_hub_firmware_for_new_space() -> None:
    space = _hub_space()
    coord = _coordinator_double(space)
    builder = await _capture_space_builder("update", "async_setup_entry", coord)
    pairs = builder("s9", "s9")
    from custom_components.ajax.update import AjaxHubFirmwareUpdate

    assert len(pairs) == 1
    assert isinstance(pairs[0][1], AjaxHubFirmwareUpdate)
    # No firmware info -> nothing.
    space.hub_details.pop("firmware")
    assert builder("s9", "s9") == []


async def test_alarm_platform_still_listens_to_new_space() -> None:
    """Guard: the pre-existing alarm_control_panel wiring must stay in place."""
    space = _hub_space()
    coord = _coordinator_double(space)
    entry = SimpleNamespace(runtime_data=coord, entry_id="entry_test", async_on_unload=lambda _f: None)
    captured: dict[str, Any] = {}

    def _fake_connect(hass: Any, entry_: Any, signal: str, domain: str, add: Any, builder: Any, *, label: str) -> None:
        captured[signal] = builder

    with patch("custom_components.ajax.alarm_control_panel.connect_new_entity_signal", _fake_connect):
        from custom_components.ajax.alarm_control_panel import async_setup_entry

        await async_setup_entry(MagicMock(), entry, MagicMock())
    assert SIGNAL_NEW_SPACE in captured


# ---------------------------------------------------------------------------
# SQS init — quiet single-attempt periodic retry
# ---------------------------------------------------------------------------


def _sqs_coord() -> AjaxDataCoordinator:
    coord = object.__new__(AjaxDataCoordinator)
    coord.hass = MagicMock()
    coord.hass.config.language = "en"
    coord.config_entry = SimpleNamespace(entry_id="e1")
    coord._sqs_init_in_progress = False
    coord._sqs_initialized = False
    coord._aws_access_key_id = "AK"
    coord._aws_secret_access_key = "SK"
    coord._queue_name = "queue.fifo"
    coord.sqs_manager = None
    return coord


async def test_init_sqs_periodic_retry_is_single_and_quiet() -> None:
    coord = _sqs_coord()
    client_cls = MagicMock(side_effect=ImportError("aiobotocore required"))
    with (
        patch("custom_components.ajax._coordinator_init.SQS_AVAILABLE", True),
        patch("custom_components.ajax._coordinator_init._AjaxSQSClient", client_cls),
        patch("custom_components.ajax._coordinator_init.ir.async_create_issue") as create_issue,
        patch("custom_components.ajax._coordinator_init.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        await coord._async_init_sqs(wait_for_dependency=False)

    client_cls.assert_called_once()  # single attempt, no inner wait loop
    sleep.assert_not_awaited()
    create_issue.assert_not_called()  # the initial attempt already raised it
    assert coord._sqs_initialized is False  # stays transient -> next poll retries
    assert coord._sqs_init_in_progress is False


async def test_init_sqs_initial_attempt_waits_and_raises_issue() -> None:
    coord = _sqs_coord()
    client_cls = MagicMock(side_effect=ImportError("aiobotocore required"))
    with (
        patch("custom_components.ajax._coordinator_init.SQS_AVAILABLE", True),
        patch("custom_components.ajax._coordinator_init._AjaxSQSClient", client_cls),
        patch("custom_components.ajax._coordinator_init.SQS_AIOBOTOCORE_WAIT_ATTEMPTS", 3),
        patch("custom_components.ajax._coordinator_init.ir.async_create_issue") as create_issue,
        patch("custom_components.ajax._coordinator_init.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        await coord._async_init_sqs()

    assert client_cls.call_count == 3
    assert sleep.await_count == 3
    create_issue.assert_called_once()
    assert coord._sqs_initialized is False


async def test_init_sqs_recovers_when_dependency_lands_mid_window() -> None:
    """Second constructor attempt succeeds -> manager starts, issue cleared."""
    coord = _sqs_coord()
    good_client = MagicMock()
    client_cls = MagicMock(side_effect=[ImportError("nope"), good_client])
    manager = MagicMock()
    manager.set_language = MagicMock()
    manager.start = AsyncMock(return_value=True)
    with (
        patch("custom_components.ajax._coordinator_init.SQS_AVAILABLE", True),
        patch("custom_components.ajax._coordinator_init._AjaxSQSClient", client_cls),
        patch("custom_components.ajax._coordinator_init._SQSManager", MagicMock(return_value=manager)),
        patch("custom_components.ajax._coordinator_init.ir.async_delete_issue") as delete_issue,
        patch("custom_components.ajax._coordinator_init.asyncio.sleep", new=AsyncMock()),
    ):
        await coord._async_init_sqs()

    assert coord._sqs_initialized is True
    assert coord.sqs_manager is manager
    delete_issue.assert_called_once()
