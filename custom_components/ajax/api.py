"""Ajax API client."""
from __future__ import annotations

import hashlib
import logging
from typing import Any

import grpc

from .const import (
    API_BASE_URL,
    API_PORT,
    DEFAULT_DEVICE_MODEL,
    DEFAULT_VERSION,
    DEFAULT_OS,
    DEFAULT_APP_LABEL,
    DEFAULT_DEVICE_TYPE,
)

# Import generated protobuf stubs
from custom_components.ajax.v3.mobilegwsvc.service.login_by_password import (
    endpoint_pb2_grpc as login_pb2_grpc,
    request_pb2 as login_request_pb2,
    response_pb2 as login_response_pb2,
)
from custom_components.ajax.v3.mobilegwsvc.service.login_by_totp import (
    endpoint_pb2_grpc as totp_pb2_grpc,
    request_pb2 as totp_request_pb2,
    response_pb2 as totp_response_pb2,
)
from custom_components.ajax.v3.mobilegwsvc.service.find_user_spaces_with_pagination import (
    endpoint_pb2_grpc as spaces_pb2_grpc,
    request_pb2 as spaces_request_pb2,
    response_pb2 as spaces_response_pb2,
)
from custom_components.ajax.v3.mobilegwsvc.service.stream_light_devices import (
    endpoint_pb2_grpc as light_devices_pb2_grpc,
    request_pb2 as light_devices_request_pb2,
    response_pb2 as light_devices_response_pb2,
)
from custom_components.ajax.v3.mobilegwsvc.commonmodels.type import user_role_pb2

# Import space security service
from custom_components.ajax.systems.ajax.api.mobile.v2.space.security import (
    space_security_endpoints_pb2_grpc,
    arm_request_pb2,
    disarm_request_pb2,
    arm_to_night_mode_request_pb2,
)
from custom_components.ajax.systems.ajax.api.mobile.v2.space.security.group import (
    arm_group_request_pb2,
    disarm_group_request_pb2,
)
from custom_components.ajax.systems.ajax.api.mobile.v2.common.space import space_locator_pb2

# Import space service for panic button and streaming
from custom_components.ajax.systems.ajax.api.mobile.v2.space import (
    space_endpoints_pb2_grpc,
    press_panic_button_request_pb2,
    stream_space_updates_request_pb2,
)

# Import hub object service for Hub-specific data
from custom_components.ajax.systems.ajax.api.mobile.v2.hubobject import (
    hub_object_endpoints_pb2_grpc,
    stream_hub_object_request_pb2,
)

# Import device streaming service for real-time device updates
from custom_components.ajax.v3.mobilegwsvc.service.stream_hub_device import (
    endpoint_pb2_grpc as stream_device_pb2_grpc,
    request_pb2 as stream_device_request_pb2,
    response_pb2 as stream_device_response_pb2,
)

# Import notification log service
from custom_components.ajax.systems.ajax.api.mobile.v2.notificationlog import (
    notification_log_endpoints_pb2_grpc,
    find_notifications_pb2 as notification_find_pb2,
    stream_notification_log_pb2,
)

# Import device control services for sockets/relays
from custom_components.ajax.v3.mobilegwsvc.service.device_command_device_on import (
    endpoint_pb2_grpc as device_on_pb2_grpc,
    request_pb2 as device_on_request_pb2,
    response_pb2 as device_on_response_pb2,
)
from custom_components.ajax.v3.mobilegwsvc.service.device_command_device_off import (
    endpoint_pb2_grpc as device_off_pb2_grpc,
    request_pb2 as device_off_request_pb2,
    response_pb2 as device_off_response_pb2,
)

_LOGGER = logging.getLogger(__name__)


class AjaxApiError(Exception):
    """Base exception for Ajax API errors."""


class AjaxAuthError(AjaxApiError):
    """Exception for authentication errors."""


class Ajax2FARequiredError(AjaxAuthError):
    """Exception for when two-factor authentication is required."""

    def __init__(self, request_id: str) -> None:
        """Initialize with request_id for subsequent 2FA login."""
        super().__init__("Two-factor authentication required")
        self.request_id = request_id


class AjaxConnectionError(AjaxApiError):
    """Exception for temporary network connection errors."""


class AjaxApi:
    """Ajax API client using gRPC."""

    def __init__(
        self,
        email: str,
        password: str,
        device_id: str,
        device_model: str = DEFAULT_DEVICE_MODEL,
        password_is_hashed: bool = False,
        session_token: str | None = None,
    ) -> None:
        """Initialize the Ajax API client.

        Args:
            email: User email
            password: User password (plain text or SHA256 hash if password_is_hashed=True)
            device_id: Device identifier
            device_model: Device model name
            password_is_hashed: If True, password is already SHA256 hashed
            session_token: Optional session token from previous authentication (hex string)
        """
        self.email = email
        self.password = password
        self.device_id = device_id
        self.device_model = device_model
        self.password_is_hashed = password_is_hashed

        # Restore session token if provided (convert from hex string to bytes)
        self.session_token: bytes | None = bytes.fromhex(session_token) if session_token else None
        self.user_id: str | None = None
        self.user_name: str | None = None

        # Create gRPC channel
        credentials = grpc.ssl_channel_credentials()
        self.channel = grpc.aio.secure_channel(
            f"{API_BASE_URL}:{API_PORT}",
            credentials,
        )

    def _get_metadata(self, include_auth: bool = False) -> list[tuple[str, str]]:
        """Get gRPC metadata headers."""
        metadata = [
            ("user-agent", "grpc-java-okhttp/1.75.0"),
            ("content-type", "application/grpc"),
            ("client-device-id", self.device_id),
            ("client-device-model", self.device_model),
            ("client-version-major", DEFAULT_VERSION),
            ("client-os", DEFAULT_OS),
            ("application-label", DEFAULT_APP_LABEL),
            ("client-device-type", DEFAULT_DEVICE_TYPE),
        ]

        if include_auth and self.session_token:
            metadata.extend([
                ("client-session-token", self.session_token.hex()),
                ("a911-user-id", self.user_id or ""),
                ("client-user-login", self.email),
                ("client-user-role", "USER"),
            ])

        return metadata

    async def async_login(self) -> dict[str, Any]:
        """Login to Ajax and get session token."""
        try:
            # Hash password with SHA256 (required by Ajax API protocol)
            # Note: SHA256 is mandated by Ajax Systems API, not our choice
            # The password is transmitted as SHA256 hash over TLS
            # If password is already hashed (from config entry), use it directly
            if self.password_is_hashed:
                password_hash = self.password
            else:
                password_hash = hashlib.sha256(self.password.encode()).hexdigest()  # nosec B324

            # Create login request
            request = login_request_pb2.LoginByPasswordRequest(
                email=self.email,
                password_sha256_hash=password_hash,
                user_role=user_role_pb2.USER_ROLE_USER,
            )

            # Create stub and call login service
            stub = login_pb2_grpc.LoginByPasswordServiceStub(self.channel)
            response = await stub.execute(request, metadata=self._get_metadata())

            # Check response type
            if response.HasField("success"):
                # Extract session token and user info
                self.session_token = response.success.session_token

                account = response.success.lite_account
                if account:
                    self.user_id = account.user_hex_id
                    # Build full name from first_name and last_name
                    name_parts = []
                    if account.first_name:
                        name_parts.append(account.first_name)
                    if account.last_name:
                        name_parts.append(account.last_name)
                    self.user_name = " ".join(name_parts) if name_parts else account.email
                else:
                    self.user_id = None
                    self.user_name = self.email

                _LOGGER.info("Successfully logged in as %s (ID: %s)", self.user_name, self.user_id)

                return {
                    "session_token": self.session_token.hex(),
                    "user_id": self.user_id,
                    "user_name": self.user_name,
                }
            elif response.HasField("failure"):
                # Handle various failure cases
                failure = response.failure
                if failure.HasField("invalid_credentials"):
                    raise AjaxAuthError("Invalid email or password")
                elif failure.HasField("account_not_confirmed"):
                    raise AjaxAuthError("Account not confirmed")
                elif failure.HasField("account_locked"):
                    raise AjaxAuthError("Account is locked")
                elif failure.HasField("two_fa_required"):
                    # Extract request_id for TOTP login
                    request_id = failure.two_fa_required.request_id
                    _LOGGER.info("Two-factor authentication required (request_id: %s)", request_id)
                    raise Ajax2FARequiredError(request_id)
                else:
                    raise AjaxAuthError("Login failed: Unknown error")
            else:
                raise AjaxApiError("Invalid login response")

        except grpc.RpcError as err:
            _LOGGER.error("gRPC error during login: %s", err)
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AjaxAuthError("Invalid credentials") from err
            raise AjaxApiError(f"gRPC error: {err}") from err
        except (AjaxAuthError, AjaxApiError):
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error during login")
            raise AjaxApiError(f"Login failed: {err}") from err

    async def async_login_with_totp(self, request_id: str, totp_code: str) -> dict[str, Any]:
        """Complete login with TOTP (two-factor authentication) code.

        Args:
            request_id: The request_id received from async_login when 2FA was required
            totp_code: The 6-digit TOTP code from the authenticator app

        Returns:
            Dictionary with session_token, user_id, and user_name

        Raises:
            AjaxAuthError: If the TOTP code is invalid or authentication fails
            AjaxApiError: If there's a communication error
        """
        try:
            # Create TOTP login request
            request = totp_request_pb2.LoginByTotpRequest(
                email=self.email,
                user_role=user_role_pb2.USER_ROLE_USER,
                totp=totp_code,
                request_id=request_id,
            )

            # Create stub and call TOTP login service
            stub = totp_pb2_grpc.LoginByTotpServiceStub(self.channel)
            response = await stub.execute(request, metadata=self._get_metadata())

            # Check response type
            if response.HasField("success"):
                # Extract session token and user info
                self.session_token = response.success.session_token

                account = response.success.lite_account
                if account:
                    self.user_id = account.user_hex_id
                    # Build full name from first_name and last_name
                    name_parts = []
                    if account.first_name:
                        name_parts.append(account.first_name)
                    if account.last_name:
                        name_parts.append(account.last_name)
                    self.user_name = " ".join(name_parts) if name_parts else account.email
                else:
                    self.user_id = None
                    self.user_name = self.email

                _LOGGER.info("Successfully logged in with TOTP as %s (ID: %s)", self.user_name, self.user_id)

                return {
                    "session_token": self.session_token.hex(),
                    "user_id": self.user_id,
                    "user_name": self.user_name,
                }
            elif response.HasField("failure"):
                # Handle various failure cases
                failure = response.failure
                if failure.HasField("invalid_totp"):
                    raise AjaxAuthError("Invalid verification code")
                elif failure.HasField("account_not_confirmed"):
                    raise AjaxAuthError("Account not confirmed")
                elif failure.HasField("account_locked"):
                    raise AjaxAuthError("Account is locked")
                elif failure.HasField("bad_request"):
                    raise AjaxAuthError("Invalid request")
                else:
                    raise AjaxAuthError("TOTP login failed: Unknown error")
            else:
                raise AjaxApiError("Invalid TOTP login response")

        except grpc.RpcError as err:
            _LOGGER.error("gRPC error during TOTP login: %s", err)
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AjaxAuthError("Invalid verification code") from err
            raise AjaxApiError(f"gRPC error: {err}") from err
        except (AjaxAuthError, AjaxApiError):
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error during TOTP login")
            raise AjaxApiError(f"TOTP login failed: {err}") from err

    async def async_get_account(self) -> dict[str, Any]:
        """Get account information."""
        if not self.session_token:
            raise AjaxAuthError("Not authenticated. Call async_login() first.")

        # Return cached info from login
        return {
            "user_hex_id": self.user_id,
            "name": self.user_name,
            "email": self.email,
        }

    async def async_get_spaces(self) -> list[dict[str, Any]]:
        """Get list of user spaces (hubs/systems)."""
        if not self.session_token:
            raise AjaxAuthError("Not authenticated. Call async_login() first.")

        try:
            # Create request for finding user spaces
            # Structure: limit (int), pagination (token), search_phrase (str)
            request = spaces_request_pb2.FindUserSpacesWithPaginationRequest(
                limit=100,  # Get up to 100 spaces
                search_phrase="",  # No search filter
            )

            # Create stub and call service
            stub = spaces_pb2_grpc.FindUserSpacesWithPaginationServiceStub(self.channel)
            response = await stub.execute(
                request, metadata=self._get_metadata(include_auth=True)
            )

            spaces = []
            if response.HasField("success"):
                for space in response.success.spaces:
                    space_data = {
                        "id": space.id if hasattr(space, "id") else "",
                        "name": space.profile.name if hasattr(space, "profile") and hasattr(space.profile, "name") else "Unknown",
                    }

                    # Add hub_id if available
                    if hasattr(space, "hub_id") and space.hub_id:
                        space_data["hub_id"] = space.hub_id

                    # Parse security state if available
                    if hasattr(space, "security_state"):
                        security_state_int = int(space.security_state)
                        # Map DisplayedSpaceSecurityState enum to string
                        # 0=NONE, 1=ARMED, 2=DISARMED, 3=NIGHT_MODE, 4=PARTIALLY_ARMED, etc.
                        security_state_map = {
                            0: "NONE",
                            1: "ARMED",
                            2: "DISARMED",
                            3: "NIGHT_MODE",
                            4: "PARTIALLY_ARMED",
                            5: "AWAITING_APP_EXIT_TIMER",
                            6: "AWAITING_SECOND_STAGE_CONFIRMATION",
                            7: "TWO_STAGE_ARMING_INCOMPLETE",
                            8: "AWAITING_VDS_ARMING_COMPLETION",
                        }
                        security_state_str = security_state_map.get(security_state_int, "NONE")
                        space_data["security_state"] = security_state_str
                        _LOGGER.debug("Space %s initial security state: %s", space_data["name"], security_state_str)

                    spaces.append(space_data)

                _LOGGER.info("Found %d spaces", len(spaces))
                return spaces
            elif response.HasField("failure"):
                _LOGGER.error("Failed to get spaces: %s", response.failure)
                raise AjaxApiError("Failed to get spaces")
            else:
                _LOGGER.warning("Empty response when getting spaces")
                return []

        except grpc.RpcError as err:
            _LOGGER.error("gRPC error getting spaces: %s", err)
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AjaxAuthError("Session expired") from err
            raise AjaxApiError(f"gRPC error: {err}") from err
        except (AjaxAuthError, AjaxApiError):
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error getting spaces")
            raise AjaxApiError(f"Failed to get spaces: {err}") from err

    async def async_get_devices(self, space_id: str) -> list[dict[str, Any]]:
        """Get list of devices for a given space."""
        if not self.session_token:
            raise AjaxAuthError("Not authenticated. Call async_login() first.")

        try:
            # Create request for streaming light devices
            request = light_devices_request_pb2.StreamLightDevicesRequest(
                space_id=space_id
            )

            # Create stub and call service (streaming response)
            stub = light_devices_pb2_grpc.StreamLightDevicesServiceStub(self.channel)
            response_stream = stub.execute(
                request, metadata=self._get_metadata(include_auth=True)
            )

            devices = []
            response_count = 0

            # Read the stream - first message should be a snapshot
            async for response in response_stream:
                response_count += 1

                if response.HasField("success"):
                    if response.success.HasField("snapshot"):
                        snapshot = response.success.snapshot
                        _LOGGER.info(
                            "Received device snapshot with %d light_devices",
                            len(snapshot.light_devices)
                        )

                        # Process snapshot with all devices
                        for idx, light_device in enumerate(snapshot.light_devices):
                            device_data = self._parse_light_device(light_device)
                            if device_data:
                                devices.append(device_data)

                        # After receiving snapshot, we can break (updates would follow in real streaming)
                        break
                    elif response.success.HasField("updates"):
                        # Skip updates for now - we only need initial snapshot
                        continue
                elif response.HasField("failure"):
                    _LOGGER.error("Failed to get devices: %s", response.failure)
                    raise AjaxApiError("Failed to get devices")
                else:
                    _LOGGER.warning("Unknown response type received")

            _LOGGER.info("Found %d devices in space %s", len(devices), space_id)
            return devices

        except grpc.RpcError as err:
            _LOGGER.error("gRPC error getting devices: %s", err)
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AjaxAuthError("Session expired") from err
            raise AjaxApiError(f"gRPC error: {err}") from err
        except (AjaxAuthError, AjaxApiError):
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error getting devices")
            raise AjaxApiError(f"Failed to get devices: {err}") from err

    async def async_stream_hub_object(self, hub_id: str) -> dict[str, Any] | None:
        """Stream Hub object data to get detailed Hub information."""
        if not self.session_token:
            raise AjaxAuthError("Not authenticated. Call async_login() first.")

        try:
            # Create request for streaming hub object
            request = stream_hub_object_request_pb2.StreamHubObjectRequest(
                hex_id=hub_id
            )

            # Create stub and call service (streaming response)
            stub = hub_object_endpoints_pb2_grpc.HubObjectServiceStub(self.channel)
            response_stream = stub.streamHubObject(
                request, metadata=self._get_metadata(include_auth=True)
            )

            hub_data = None
            response_count = 0

            # Read the stream - first message should be a snapshot
            async for response in response_stream:
                response_count += 1

                # Check which type of response (snapshot, update, create, delete)
                which = response.WhichOneof('item')

                if which in ('snapshot', 'update', 'create'):
                    hub_obj = getattr(response, which)
                    _LOGGER.info("Received HubObject %s for hub %s", which, hub_id)

                    # Parse the HubObject
                    hub_data = self._parse_hub_object(hub_obj)

                    # For snapshot, we can break after first response
                    # For update/create, we continue streaming for real-time updates
                    if which == 'snapshot':
                        break

                elif which == 'delete':
                    _LOGGER.warning("Hub %s was deleted", hub_id)
                    return None

            if response_count == 0:
                _LOGGER.warning("No responses received from streamHubObject")
                return None

            return hub_data

        except grpc.RpcError as err:
            _LOGGER.error("gRPC error streaming hub object: %s", err)
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AjaxAuthError("Session expired") from err
            raise AjaxApiError(f"gRPC error: {err}") from err
        except (AjaxAuthError, AjaxApiError):
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error streaming hub object")
            raise AjaxApiError(f"Failed to stream hub object: {err}") from err

    def _parse_hub_object(self, hub_obj) -> dict[str, Any]:
        """Parse a HubObject protobuf message to a dict."""
        try:
            hub_data = {
                "hex_id": hub_obj.hex_id if hasattr(hub_obj, "hex_id") else None,
            }

            # Parse SIM card info
            try:
                if hasattr(hub_obj, "sim_card"):
                    sim = hub_obj.sim_card
                    hub_data["sim_card"] = {
                        "active_sim_card": sim.active_sim_card if hasattr(sim, "active_sim_card") else None,
                        "sim_card_status": str(sim.sim_card_status).split("_")[-1] if hasattr(sim, "sim_card_status") else None,
                        "imei": sim.imei if hasattr(sim, "imei") else None,
                    }
            except (ValueError, AttributeError) as e:
                _LOGGER.debug("Could not parse SIM card info: %s", e)

            # Parse firmware update info
            # Note: system_firmware_update indicates auto-update is enabled, not that an update is available
            try:
                if hasattr(hub_obj, "system_firmware_update"):
                    fw = hub_obj.system_firmware_update
                    hub_data["system_firmware_update"] = {
                        "enabled": True,
                        "version": fw.firmware_version if hasattr(fw, "firmware_version") else None,
                    }
            except (ValueError, AttributeError) as e:
                _LOGGER.debug("Could not parse system firmware update info: %s", e)

            # Parse device firmware updates
            try:
                if hasattr(hub_obj, "device_firmware_updates") and hub_obj.device_firmware_updates:
                    updates = hub_obj.device_firmware_updates
                    if hasattr(updates, "device_firmware_update"):
                        update_list = []
                        for update in updates.device_firmware_update:
                            update_info = {
                                "device_id": update.device_id if hasattr(update, "device_id") else None,
                                "is_critical": update.is_critical.value if hasattr(update, "is_critical") and update.is_critical else False,
                            }
                            # Parse status
                            if hasattr(update, "status"):
                                status_type = update.status.WhichOneof("status")
                                update_info["status"] = status_type
                            update_list.append(update_info)

                        if update_list:
                            hub_data["pending_firmware_updates"] = update_list
                            hub_data["has_firmware_updates"] = True
            except (ValueError, AttributeError) as e:
                _LOGGER.debug("Could not parse device firmware updates: %s", e)

            # Parse hub connection properties
            try:
                if hasattr(hub_obj, "hub_connection_properties") and hub_obj.hub_connection_properties:
                    props = hub_obj.hub_connection_properties
                    conn_data = {}
                    # Parse delay durations for offline detection
                    if hasattr(props, "delay_before_hub_goes_offline"):
                        delay = props.delay_before_hub_goes_offline
                        if hasattr(delay, "seconds"):
                            conn_data["offline_delay_seconds"] = delay.seconds

                    if hasattr(props, "delay_before_hub_goes_online"):
                        delay = props.delay_before_hub_goes_online
                        if hasattr(delay, "seconds"):
                            conn_data["online_delay_seconds"] = delay.seconds

                    if conn_data:
                        hub_data["connection_properties"] = conn_data
            except (ValueError, AttributeError) as e:
                _LOGGER.debug("Could not parse hub connection properties: %s", e)

            # Parse installation companies
            try:
                if hasattr(hub_obj, "installation_companies") and hub_obj.installation_companies:
                    companies = hub_obj.installation_companies
                    if hasattr(companies, "installation_company"):
                        company_list = []
                        for company in companies.installation_company:
                            company_info = {}
                            if hasattr(company, "company_id"):
                                company_info["id"] = company.company_id
                            if hasattr(company, "company_name"):
                                company_info["name"] = company.company_name
                            if company_info:
                                company_list.append(company_info)

                        if company_list:
                            hub_data["installation_companies"] = company_list
            except (ValueError, AttributeError) as e:
                _LOGGER.debug("Could not parse installation companies: %s", e)

            # Parse monitoring companies
            try:
                if hasattr(hub_obj, "monitoring_companies") and hub_obj.monitoring_companies:
                    companies = hub_obj.monitoring_companies
                    if hasattr(companies, "monitoring_company"):
                        company_list = []
                        for company in companies.monitoring_company:
                            company_info = {}
                            if hasattr(company, "company_id"):
                                company_info["id"] = company.company_id
                            if hasattr(company, "company_name"):
                                company_info["name"] = company.company_name
                            if company_info:
                                company_list.append(company_info)

                        if company_list:
                            hub_data["monitoring_companies"] = company_list
            except (ValueError, AttributeError) as e:
                _LOGGER.debug("Could not parse monitoring companies: %s", e)

            return hub_data

        except Exception as err:
            _LOGGER.exception("Failed to parse HubObject: %s", err)
            return {"hex_id": None, "error": str(err)}

    def _parse_light_device(self, light_device) -> dict[str, Any] | None:
        """Parse a LightDevice protobuf message to a dict."""
        try:
            # LightDevice can be a hub_device, video_edge, video_edge_channel, or smart_lock
            if hasattr(light_device, "hub_device") and light_device.hub_device:
                hub_dev = light_device.hub_device

                # Check if common_device exists
                try:
                    common = hub_dev.common_device
                    if not common or not hasattr(common, "profile"):
                        _LOGGER.warning("hub_device has no valid common_device field")
                        return None
                except (AttributeError, ValueError) as e:
                    _LOGGER.warning("hub_device has no common_device field: %s", e)
                    return None
                profile = common.profile

                object_type_str = self._get_device_type(common.object_type)

                device_data = {
                    "id": profile.id,
                    "name": profile.name,
                    "type": object_type_str,
                    "hub_id": common.hub_id,
                    "room_id": profile.room_id if profile.room_id else None,
                    "group_id": profile.group_id if profile.group_id else None,
                    "malfunctions": profile.malfunctions,
                    "bypassed": profile.bypassed,
                    "states": [str(state).split(".")[-1] if "." in str(state) else str(state) for state in profile.states],
                    "online": True,  # TODO: Parse status to determine online state
                }

                # Add device color, label, and marketing ID
                if hasattr(profile, "device_color"):
                    device_data["device_color"] = str(profile.device_color).split("_")[-1]
                if hasattr(profile, "device_label"):
                    device_data["device_label"] = str(profile.device_label).split("_")[-1]
                if hasattr(profile, "device_marketing_id"):
                    device_data["device_marketing_id"] = profile.device_marketing_id

                # Parse statuses for battery, SIM, firmware, etc.
                device_data["battery_level"] = None
                device_data["battery_state"] = None
                device_data["firmware_version"] = None
                device_data["hardware_version"] = None

                # Store complex attributes separately
                attributes = {}

                # Track SIM cards status (for dual-SIM hubs)
                sim_cards = []

                for status in profile.statuses:
                    try:
                        # Battery status
                        if status.HasField("battery"):
                            device_data["battery_level"] = status.battery.charge_level_percentage
                            device_data["battery_state"] = str(status.battery.battery_state).split("_")[-1]

                        # SIM status
                        elif status.HasField("sim_status"):
                            # Parse individual SIM card status
                            sim_status_raw = str(status.sim_status.sim_card_status)

                            # Extract just the status name (e.g., "MISSING" from "SIM_CARD_STATUS_MISSING")
                            if "SIM_CARD_STATUS_" in sim_status_raw:
                                sim_status_str = sim_status_raw.split("SIM_CARD_STATUS_")[-1]
                            else:
                                sim_status_str = sim_status_raw

                            sim_info = {
                                "status": sim_status_str,
                                "installed": sim_status_str not in ["MISSING", "NOT_INSTALLED", "2"],
                            }

                            # Check if there's a slot number or if this is aggregated data
                            if hasattr(status.sim_status, "slot"):
                                sim_info["slot"] = status.sim_status.slot

                            sim_cards.append(sim_info)

                        # GSM status (for hubs with cellular connection)
                        elif status.HasField("gsm_status"):
                            gsm = status.gsm_status
                            gsm_data = {}

                            # Parse GSM type (2G, 3G, 4G, etc.)
                            if hasattr(gsm, "type"):
                                gsm_type_raw = str(gsm.type)
                                # Handle both text and numeric enum formats
                                if "GSM_TYPE_" in gsm_type_raw:
                                    gsm_type_str = gsm_type_raw.split("GSM_TYPE_")[-1]
                                elif gsm_type_raw.isdigit():
                                    # Numeric enum: 0=UNSPECIFIED, 1=2G, 2=3G, 3=4G, 4=5G
                                    gsm_type_map = {0: "UNKNOWN", 1: "2G", 2: "3G", 3: "4G", 4: "5G"}
                                    gsm_type_str = gsm_type_map.get(int(gsm_type_raw), gsm_type_raw)
                                else:
                                    gsm_type_str = gsm_type_raw.split("_")[-1]

                                gsm_data["type"] = gsm_type_str
                                attributes["gsm_type"] = gsm_type_str

                            # Parse GSM connection status
                            if hasattr(gsm, "status"):
                                gsm_status_raw = str(gsm.status)
                                # Handle both text and numeric enum formats
                                if "STATUS_" in gsm_status_raw:
                                    gsm_status_str = gsm_status_raw.split("STATUS_")[-1]
                                elif gsm_status_raw.isdigit():
                                    # Numeric enum: 0=DISCONNECTED, 1=CONNECTING, 2=CONNECTED, 3=ERROR
                                    gsm_status_map = {
                                        0: "DISCONNECTED",
                                        1: "CONNECTING",
                                        2: "CONNECTED",
                                        3: "ERROR"
                                    }
                                    gsm_status_str = gsm_status_map.get(int(gsm_status_raw), gsm_status_raw)
                                else:
                                    gsm_status_str = gsm_status_raw.split("_")[-1]

                                gsm_data["status"] = gsm_status_str
                                attributes["gsm_connection_status"] = gsm_status_str

                            if gsm_data:
                                attributes["gsm_info"] = gsm_data

                        # Temperature (simple ValueStatus)
                        elif status.HasField("temperature"):
                            # Temperature is in degrees Celsius
                            attributes["temperature"] = status.temperature.value

                        # Life Quality (temperature, humidity, CO2)
                        elif status.HasField("life_quality"):
                            lq = status.life_quality
                            # Values are in tenths (e.g., 225 = 22.5°C, 450 = 45.0%)
                            if hasattr(lq, "actual_temperature") and lq.actual_temperature:
                                attributes["temperature"] = lq.actual_temperature / 10.0
                            if hasattr(lq, "actual_humidity") and lq.actual_humidity:
                                attributes["humidity"] = lq.actual_humidity / 10.0
                            if hasattr(lq, "actual_co2") and lq.actual_co2:
                                attributes["co2"] = lq.actual_co2

                        # Door/window state
                        elif status.HasField("door_opened"):
                            attributes["door_opened"] = True

                        # Motion detection
                        elif status.HasField("motion_detected"):
                            if hasattr(status.motion_detected, "detected_at"):
                                attributes["motion_detected"] = True
                                attributes["motion_detected_at"] = str(status.motion_detected.detected_at)

                        # Always active (device active even in night mode)
                        elif status.HasField("always_active"):
                            attributes["always_active"] = True

                        # Armed in night mode
                        elif status.HasField("armed_in_night_mode"):
                            attributes["armed_in_night_mode"] = True

                        # Smoke detection
                        elif status.HasField("smoke_detected"):
                            attributes["smoke_detected"] = True

                        # Leak detection
                        elif status.HasField("leak_detected"):
                            attributes["leak_detected"] = True

                        # Tamper (case drilling)
                        elif status.HasField("case_drilling_detected"):
                            attributes["tampered"] = True

                        # Signal strength
                        elif status.HasField("signal_strength"):
                            if hasattr(status.signal_strength, "device_signal_level"):
                                signal_str = str(status.signal_strength.device_signal_level).split("_")[-1]
                                # Convert to percentage estimate
                                signal_map = {"WEAK": 25, "NORMAL": 50, "STRONG": 75, "EXCELLENT": 100}
                                device_data["signal_strength"] = signal_map.get(signal_str, 50)
                                attributes["signal_level"] = signal_str

                    except (ValueError, AttributeError) as e:
                        # Some status fields may not exist on all devices
                        _LOGGER.debug("Error parsing status field: %s", e)
                        pass

                    # Check for firmware/hardware without HasField (safer)
                    try:
                        if hasattr(status, "firmware") and status.firmware:
                            if hasattr(status.firmware, "version"):
                                device_data["firmware_version"] = status.firmware.version
                    except (AttributeError, ValueError):
                        pass

                    try:
                        if hasattr(status, "hardware") and status.hardware:
                            if hasattr(status.hardware, "version"):
                                device_data["hardware_version"] = status.hardware.version
                    except (AttributeError, ValueError):
                        pass

                # Process SIM cards status
                if sim_cards:
                    installed_count = sum(1 for sim in sim_cards if sim["installed"])

                    # Detect total SIM slots based on device type
                    # Hub 2 Plus and Hub 2 (4G) have 2 SIM slots
                    device_type_lower = object_type_str.lower()
                    if "hub" in device_type_lower and ("plus" in device_type_lower or "4g" in device_type_lower or "two" in device_type_lower):
                        expected_slots = 2
                    else:
                        expected_slots = len(sim_cards)

                    # If we only got 1 status but expect 2 slots, it means both are empty/missing
                    if len(sim_cards) == 1 and sim_cards[0]["status"] == "MISSING" and expected_slots == 2:
                        total_slots = 2
                        installed_count = 0
                    else:
                        total_slots = max(len(sim_cards), expected_slots)

                    attributes["sim_status"] = f"{installed_count}/{total_slots}"
                    attributes["sim_cards"] = sim_cards
                    attributes["sim_slots_total"] = total_slots
                    attributes["sim_slots_used"] = installed_count

                # Parse Hub-specific fields (GSM, WiFi, tamper, external power, etc.)
                if "hub" in object_type_str.lower():
                    # GSM signal and network status
                    try:
                        if hasattr(hub_dev, "gsm") and hub_dev.gsm:
                            gsm = hub_dev.gsm
                            if hasattr(gsm, "signal_level"):
                                signal_level_str = str(gsm.signal_level).split("_")[-1]
                                attributes["gsm_signal_level"] = signal_level_str
                            if hasattr(gsm, "network_status"):
                                network_status_str = str(gsm.network_status).split("_")[-1]
                                attributes["network_status"] = network_status_str
                    except (ValueError, AttributeError) as e:
                        _LOGGER.debug("Could not parse GSM data: %s", e)

                    # WiFi signal
                    try:
                        if hasattr(hub_dev, "wifi") and hub_dev.wifi:
                            wifi = hub_dev.wifi
                            if hasattr(wifi, "signal_level"):
                                wifi_signal_str = str(wifi.signal_level).split("_")[-1]
                                attributes["wifi_signal_level"] = wifi_signal_str
                    except (ValueError, AttributeError) as e:
                        _LOGGER.debug("Could not parse WiFi data: %s", e)

                    # Active channels (connection type)
                    if hasattr(hub_dev, "active_channels"):
                        channels = [str(ch).split("_")[-1] for ch in hub_dev.active_channels]
                        if channels:
                            attributes["active_connection"] = ", ".join(channels)

                    # Tamper status (cover open/closed)
                    if hasattr(hub_dev, "tampered"):
                        attributes["tampered"] = hub_dev.tampered
                        _LOGGER.debug("Tampered: %s", hub_dev.tampered)

                    # External power status
                    if hasattr(hub_dev, "externally_powered"):
                        attributes["externally_powered"] = hub_dev.externally_powered
                        _LOGGER.debug("Externally powered: %s", hub_dev.externally_powered)

                    # Noise level
                    if hasattr(hub_dev, "noise_level") and hub_dev.noise_level:
                        noise = hub_dev.noise_level
                        if hasattr(noise, "avg_value_channel1"):
                            attributes["noise_level_channel1"] = noise.avg_value_channel1
                        if hasattr(noise, "avg_value_channel2"):
                            attributes["noise_level_channel2"] = noise.avg_value_channel2
                        if hasattr(noise, "high"):
                            attributes["noise_level_high"] = noise.high
                        # Calculate average if we have channel data
                        if "noise_level_channel1" in attributes and "noise_level_channel2" in attributes:
                            avg = (attributes["noise_level_channel1"] + attributes["noise_level_channel2"]) / 2
                            attributes["noise_level_avg"] = round(avg, 1)
                            _LOGGER.debug("Noise levels: ch1=%d, ch2=%d, avg=%.1f",
                                        attributes["noise_level_channel1"],
                                        attributes["noise_level_channel2"],
                                        avg)

                    # Firmware version (from hub firmware field, not profile statuses)
                    if hasattr(hub_dev, "firmware") and hub_dev.firmware:
                        if hasattr(hub_dev.firmware, "version") and hub_dev.firmware.version:
                            device_data["firmware_version"] = hub_dev.firmware.version
                            _LOGGER.debug("Firmware version: %s", hub_dev.firmware.version)

                    # Hardware versions
                    if hasattr(hub_dev, "hardware_versions") and hub_dev.hardware_versions:
                        hw = hub_dev.hardware_versions
                        hw_parts = []
                        if hasattr(hw, "modem") and hw.modem:
                            hw_parts.append(f"Modem:{hw.modem}")
                        if hasattr(hw, "wifi") and hw.wifi:
                            hw_parts.append(f"WiFi:{hw.wifi}")
                        if hasattr(hw, "ethernet") and hw.ethernet:
                            hw_parts.append(f"Eth:{hw.ethernet}")
                        if hw_parts:
                            device_data["hardware_version"] = ", ".join(hw_parts)
                            _LOGGER.debug("Hardware versions: %s", device_data["hardware_version"])

                # Check for spread_properties and device_specific_properties
                # These might contain arming mode information or socket/relay states
                if hasattr(hub_dev, "spread_properties") and hub_dev.spread_properties:
                    _LOGGER.debug("Device %s has %d spread_properties", profile.name, len(hub_dev.spread_properties))
                    for idx, prop in enumerate(hub_dev.spread_properties):
                        _LOGGER.debug("  spread_property[%d]: %s", idx, prop)

                        # Parse socket/relay channel information
                        if hasattr(prop, "channel") and prop.channel:
                            channel = prop.channel
                            channel_data = {}

                            if hasattr(channel, "channel_id"):
                                channel_data["channel_id"] = channel.channel_id
                            if hasattr(channel, "is_channel_enabled"):
                                channel_data["is_enabled"] = channel.is_channel_enabled
                            if hasattr(channel, "is_channel_on"):
                                channel_data["is_on"] = channel.is_channel_on
                            else:
                                # If is_channel_on is missing, it means the channel is OFF
                                channel_data["is_on"] = False

                            if hasattr(channel, "output_mode"):
                                channel_data["output_mode"] = str(channel.output_mode)
                            if hasattr(channel, "operating_mode"):
                                channel_data["operating_mode"] = str(channel.operating_mode)

                            if channel_data:
                                attributes["channel"] = channel_data
                                _LOGGER.debug("Socket/Relay channel data: %s", channel_data)

                if hasattr(hub_dev, "device_specific_properties") and hub_dev.device_specific_properties:
                    _LOGGER.debug("Device %s has %d device_specific_properties", profile.name, len(hub_dev.device_specific_properties))
                    for idx, prop in enumerate(hub_dev.device_specific_properties):
                        _LOGGER.debug("  device_specific_property[%d]: %s", idx, prop)

                # Add default values for always_active and armed_in_night_mode if not set
                # (these fields only appear when True, but we want to show them as False when absent)
                # Note: These attributes only apply to detectors, not to hubs
                if "hub" not in object_type_str.lower():
                    if "always_active" not in attributes:
                        attributes["always_active"] = False
                    if "armed_in_night_mode" not in attributes:
                        attributes["armed_in_night_mode"] = False

                # Add default value for door_opened if not set
                # (door_opened status only appears when door is open, absent when closed)
                if "door" in object_type_str.lower() or "contact" in object_type_str.lower() or object_type_str == "wire_input":
                    if "door_opened" not in attributes:
                        attributes["door_opened"] = False

                # Add attributes dict if we have any
                if attributes:
                    device_data["attributes"] = attributes

                return device_data

            elif light_device.HasField("video_edge"):
                _LOGGER.debug("Found video_edge device (not yet implemented)")
                return None
            elif light_device.HasField("video_edge_channel"):
                _LOGGER.debug("Found video_edge_channel device (not yet implemented)")
                return None
            elif light_device.HasField("smart_lock"):
                _LOGGER.debug("Found smart_lock device (not yet implemented)")
                return None
            else:
                _LOGGER.warning("Unknown LightDevice type: %s", light_device)
                return None

        except Exception as err:
            _LOGGER.exception("Failed to parse light device: %s", err)
            return None

    def _get_device_type(self, object_type) -> str:
        """Convert protobuf ObjectType to string."""
        # Map object type enum to friendly names
        # For now, return the string representation
        return str(object_type).split(".")[-1].lower() if object_type else "unknown"

    async def async_arm(self, space_id: str, force: bool = False) -> None:
        """Arm the security system.

        Args:
            space_id: The space ID to arm
            force: If True, ignore alarms and force arm even with open sensors or problems
        """
        if not self.session_token:
            raise AjaxAuthError("Not authenticated")

        try:
            _LOGGER.debug("Arming space: %s (force=%s)", space_id, force)

            # Create space locator
            space_locator = space_locator_pb2.SpaceLocator(space_id=space_id)

            # Create arm request
            request = arm_request_pb2.ArmSpaceRequest(
                space_locator=space_locator,
                ignore_alarms=force,
            )

            # Create stub and call service
            stub = space_security_endpoints_pb2_grpc.SpaceSecurityServiceStub(self.channel)
            response = await stub.arm(request, metadata=self._get_metadata(include_auth=True))

            # Check response
            if response.HasField("success"):
                _LOGGER.debug("Successfully armed space %s", space_id)
            elif response.HasField("failure"):
                failure = response.failure
                error_msg = "Unknown error"

                if failure.HasField("hub_offline"):
                    error_msg = "Hub is offline"
                elif failure.HasField("hub_busy"):
                    error_msg = "Hub is busy"
                elif failure.HasField("hub_detected_malfunctions"):
                    error_msg = "Hub detected malfunctions"
                elif failure.HasField("permission_denied"):
                    error_msg = "Permission denied"
                elif failure.HasField("already_in_the_requested_security_state"):
                    # This is not really an error - just log and return success
                    _LOGGER.debug("Space %s is already armed", space_id)
                    return
                elif failure.HasField("space_locked"):
                    error_msg = "Space is locked"

                _LOGGER.error("Failed to arm space: %s", error_msg)
                raise AjaxApiError(f"Failed to arm: {error_msg}")

        except grpc.RpcError as err:
            _LOGGER.error("gRPC error arming system: %s", err)
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AjaxAuthError("Session expired") from err
            raise AjaxApiError(f"gRPC error: {err}") from err

    async def async_disarm(self, space_id: str) -> None:
        """Disarm the security system."""
        if not self.session_token:
            raise AjaxAuthError("Not authenticated")

        try:
            _LOGGER.debug("Disarming space: %s", space_id)

            # Create space locator
            space_locator = space_locator_pb2.SpaceLocator(space_id=space_id)

            # Create disarm request
            request = disarm_request_pb2.DisarmSpaceRequest(
                space_locator=space_locator,
            )

            # Create stub and call service
            stub = space_security_endpoints_pb2_grpc.SpaceSecurityServiceStub(self.channel)
            response = await stub.disarm(request, metadata=self._get_metadata(include_auth=True))

            # Check response
            if response.HasField("success"):
                _LOGGER.debug("Successfully disarmed space %s", space_id)
            elif response.HasField("failure"):
                failure = response.failure
                error_msg = "Unknown error"

                if failure.HasField("hub_offline"):
                    error_msg = "Hub is offline"
                elif failure.HasField("hub_busy"):
                    error_msg = "Hub is busy"
                elif failure.HasField("permission_denied"):
                    error_msg = "Permission denied"
                elif failure.HasField("already_in_the_requested_security_state"):
                    # This is not really an error - just log and return success
                    _LOGGER.debug("Space %s is already disarmed", space_id)
                    return
                elif failure.HasField("space_locked"):
                    error_msg = "Space is locked"

                _LOGGER.error("Failed to disarm space: %s", error_msg)
                raise AjaxApiError(f"Failed to disarm: {error_msg}")

        except grpc.RpcError as err:
            _LOGGER.error("gRPC error disarming system: %s", err)
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AjaxAuthError("Session expired") from err
            raise AjaxApiError(f"gRPC error: {err}") from err

    async def async_arm_night_mode(self, space_id: str, force: bool = False) -> None:
        """Activate night mode.

        Args:
            space_id: The space ID to arm in night mode
            force: If True, ignore alarms and force arm even with open sensors or problems
        """
        if not self.session_token:
            raise AjaxAuthError("Not authenticated")

        try:
            _LOGGER.info("Arming space to night mode: %s (force=%s)", space_id, force)

            # Create space locator
            space_locator = space_locator_pb2.SpaceLocator(space_id=space_id)

            # Create arm to night mode request
            request = arm_to_night_mode_request_pb2.ArmSpaceToNightModeRequest(
                space_locator=space_locator,
                ignore_alarms=force,
            )

            # Create stub and call service
            stub = space_security_endpoints_pb2_grpc.SpaceSecurityServiceStub(self.channel)
            response = await stub.armToNightMode(request, metadata=self._get_metadata(include_auth=True))

            # Check response
            if response.HasField("success"):
                _LOGGER.debug("Successfully armed space %s to night mode", space_id)
            elif response.HasField("failure"):
                failure = response.failure
                error_msg = "Unknown error"

                if failure.HasField("hub_offline"):
                    error_msg = "Hub is offline"
                elif failure.HasField("hub_busy"):
                    error_msg = "Hub is busy"
                elif failure.HasField("hub_detected_malfunctions"):
                    error_msg = "Hub detected malfunctions"
                elif failure.HasField("permission_denied"):
                    error_msg = "Permission denied"
                elif failure.HasField("already_in_the_requested_security_state"):
                    # This is not really an error - just log and return success
                    _LOGGER.info("Space %s is already in night mode", space_id)
                    return
                elif failure.HasField("space_locked"):
                    error_msg = "Space is locked"

                _LOGGER.error("Failed to arm space to night mode: %s", error_msg)
                raise AjaxApiError(f"Failed to activate night mode: {error_msg}")

        except grpc.RpcError as err:
            _LOGGER.error("gRPC error activating night mode: %s", err)
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AjaxAuthError("Session expired") from err
            raise AjaxApiError(f"gRPC error: {err}") from err

    async def async_arm_group(self, space_id: str, group_id: str, force: bool = False) -> None:
        """Arm a specific group/zone.

        Args:
            space_id: The space ID
            group_id: The group ID to arm
            force: If True, ignore alarms and force arm even with open sensors or problems
        """
        if not self.session_token:
            raise AjaxAuthError("Not authenticated")

        try:
            _LOGGER.debug("Arming group %s in space %s (force=%s)", group_id, space_id, force)

            # Create space locator
            space_locator = space_locator_pb2.SpaceLocator(space_id=space_id)

            # Create arm group request
            request = arm_group_request_pb2.ArmSpaceGroupRequest(
                space_locator=space_locator,
                group_id=group_id,
                ignore_alarms=force,
            )

            # Create stub and call service
            stub = space_security_endpoints_pb2_grpc.SpaceSecurityServiceStub(self.channel)
            response = await stub.armGroup(request, metadata=self._get_metadata(include_auth=True))

            # Check response
            if response.HasField("success"):
                _LOGGER.debug("Successfully armed group %s in space %s", group_id, space_id)
            elif response.HasField("failure"):
                failure = response.failure
                error_msg = "Unknown error"

                if failure.HasField("hub_offline"):
                    error_msg = "Hub is offline"
                elif failure.HasField("hub_busy"):
                    error_msg = "Hub is busy"
                elif failure.HasField("hub_detected_malfunctions"):
                    error_msg = "Hub detected malfunctions"
                elif failure.HasField("permission_denied"):
                    error_msg = "Permission denied"
                elif failure.HasField("already_in_the_requested_security_state"):
                    _LOGGER.debug("Group %s is already armed", group_id)
                    return
                elif failure.HasField("space_locked"):
                    error_msg = "Space is locked"
                elif failure.HasField("group_not_found"):
                    error_msg = "Group not found"

                _LOGGER.error("Failed to arm group: %s", error_msg)
                raise AjaxApiError(f"Failed to arm group: {error_msg}")

        except grpc.RpcError as err:
            _LOGGER.error("gRPC error arming group: %s", err)
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AjaxAuthError("Session expired") from err
            raise AjaxApiError(f"gRPC error: {err}") from err

    async def async_disarm_group(self, space_id: str, group_id: str) -> None:
        """Disarm a specific group/zone.

        Args:
            space_id: The space ID
            group_id: The group ID to disarm
        """
        if not self.session_token:
            raise AjaxAuthError("Not authenticated")

        try:
            _LOGGER.debug("Disarming group %s in space %s", group_id, space_id)

            # Create space locator
            space_locator = space_locator_pb2.SpaceLocator(space_id=space_id)

            # Create disarm group request
            request = disarm_group_request_pb2.DisarmSpaceGroupRequest(
                space_locator=space_locator,
                group_id=group_id,
            )

            # Create stub and call service
            stub = space_security_endpoints_pb2_grpc.SpaceSecurityServiceStub(self.channel)
            response = await stub.disarmGroup(request, metadata=self._get_metadata(include_auth=True))

            # Check response
            if response.HasField("success"):
                _LOGGER.debug("Successfully disarmed group %s in space %s", group_id, space_id)
            elif response.HasField("failure"):
                failure = response.failure
                error_msg = "Unknown error"

                if failure.HasField("hub_offline"):
                    error_msg = "Hub is offline"
                elif failure.HasField("hub_busy"):
                    error_msg = "Hub is busy"
                elif failure.HasField("permission_denied"):
                    error_msg = "Permission denied"
                elif failure.HasField("already_in_the_requested_security_state"):
                    _LOGGER.debug("Group %s is already disarmed", group_id)
                    return
                elif failure.HasField("space_locked"):
                    error_msg = "Space is locked"
                elif failure.HasField("group_not_found"):
                    error_msg = "Group not found"

                _LOGGER.error("Failed to disarm group: %s", error_msg)
                raise AjaxApiError(f"Failed to disarm group: {error_msg}")

        except grpc.RpcError as err:
            _LOGGER.error("gRPC error disarming group: %s", err)
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AjaxAuthError("Session expired") from err
            raise AjaxApiError(f"gRPC error: {err}") from err

    async def async_press_panic_button(self, space_id: str) -> None:
        """Press the panic button (trigger panic alarm)."""
        if not self.session_token:
            raise AjaxAuthError("Not authenticated")

        try:
            _LOGGER.warning("PANIC BUTTON pressed for space: %s", space_id)

            # Create space locator
            space_locator = space_locator_pb2.SpaceLocator(space_id=space_id)

            # Create panic button request (optionally with GPS location)
            request = press_panic_button_request_pb2.PressPanicButtonRequest(
                space_locator=space_locator,
                # location is optional - we don't send it for privacy/simplicity
            )

            # Create stub and call service
            stub = space_endpoints_pb2_grpc.SpaceServiceStub(self.channel)
            response = await stub.pressPanicButton(request, metadata=self._get_metadata(include_auth=True))

            # Check response
            if response.HasField("success"):
                _LOGGER.warning("Successfully triggered PANIC for space %s", space_id)
            elif response.HasField("failure"):
                failure = response.failure
                error_msg = "Unknown error"

                if failure.HasField("bad_request"):
                    error_msg = "Bad request"
                elif failure.HasField("permissions_denied"):
                    error_msg = "Permission denied"
                elif failure.HasField("space_not_found"):
                    error_msg = "Space not found"
                elif failure.HasField("action_is_limited_in_empty_space"):
                    error_msg = "Action is limited in empty space"
                elif failure.HasField("hub_not_allowed_to_perform_command"):
                    error_msg = "Hub not allowed to perform command"

                _LOGGER.error("Failed to trigger panic: %s", error_msg)
                raise AjaxApiError(f"Failed to trigger panic: {error_msg}")

        except grpc.RpcError as err:
            _LOGGER.error("gRPC error triggering panic: %s", err)
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AjaxAuthError("Session expired") from err
            raise AjaxApiError(f"gRPC error: {err}") from err

    async def async_stream_space_updates(self, space_id: str):
        """Stream real-time updates for a space.

        This is a generator that yields space updates as they occur.
        Should be run in a background task.
        """
        if not self.session_token:
            raise AjaxAuthError("Not authenticated")

        try:
            _LOGGER.info("Starting real-time stream for space %s", space_id)

            # Create space locator
            space_locator = space_locator_pb2.SpaceLocator(space_id=space_id)

            # Create stream request
            request = stream_space_updates_request_pb2.StreamSpaceUpdatesRequest(
                space_locator=space_locator
            )

            # Create stub and start streaming
            stub = space_endpoints_pb2_grpc.SpaceServiceStub(self.channel)
            response_stream = stub.stream(
                request, metadata=self._get_metadata(include_auth=True)
            )

            # Yield updates as they come in
            async for response in response_stream:
                if response.HasField("success"):
                    yield response.success
                elif response.HasField("failure"):
                    failure = response.failure
                    error_msg = "Unknown error"

                    if failure.HasField("bad_request"):
                        error_msg = "Bad request"
                    elif failure.HasField("space_not_found"):
                        error_msg = "Space not found"
                    elif failure.HasField("permission_denied"):
                        error_msg = "Permission denied"

                    _LOGGER.error("Stream error for space %s: %s", space_id, error_msg)
                    raise AjaxApiError(f"Stream error: {error_msg}")

        except grpc.RpcError as err:
            # Handle authentication errors
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                _LOGGER.error("Session expired for space %s", space_id)
                raise AjaxAuthError("Session expired") from err

            # Handle temporary connection errors (server unavailable, connection lost, etc.)
            elif err.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                # Don't log error here - coordinator will handle it based on retry count
                raise AjaxConnectionError("Connection lost to Ajax server") from err

            # Other gRPC errors (real problems)
            else:
                _LOGGER.error("gRPC error in space stream for %s: %s (code: %s)", space_id, err, err.code())
                raise AjaxApiError(f"gRPC error: {err}") from err

    async def async_stream_device_updates(self, hub_id: str, device_id: str):
        """Stream real-time updates for a specific device.

        This streams all updates for a specific device (motion, state changes, etc.)
        This is a generator that yields HubDevice updates as they occur.
        Should be run in a background task.

        Args:
            hub_id: The hub ID (space ID)
            device_id: The device ID to stream updates for

        Yields:
            HubDevice protobuf objects with updated device state
        """
        if not self.session_token:
            raise AjaxAuthError("Not authenticated")

        try:
            _LOGGER.info("Starting device stream for device %s in hub %s", device_id, hub_id)

            # Create stream request
            request = stream_device_request_pb2.StreamHubDeviceRequest(
                hub_id=hub_id,
                hub_device_id=device_id
            )

            # Create stub and start streaming
            stub = stream_device_pb2_grpc.StreamHubDeviceServiceStub(self.channel)
            response_stream = stub.execute(
                request, metadata=self._get_metadata(include_auth=True)
            )

            # Yield device updates as they come in
            async for response in response_stream:
                if response.HasField("success"):
                    success = response.success

                    # Yield the snapshot (complete device state)
                    if success.HasField("snapshot"):
                        hub_device = success.snapshot.hub_device
                        yield hub_device

                elif response.HasField("failure"):
                    failure = response.failure
                    error_msg = "Unknown error"

                    if failure.HasField("bad_request"):
                        error_msg = "Bad request"
                    elif failure.HasField("device_not_found"):
                        error_msg = "Device not found"

                    _LOGGER.error("Device stream error for %s: %s", device_id, error_msg)
                    raise AjaxApiError(f"Device stream error: {error_msg}")

        except grpc.RpcError as err:
            _LOGGER.error("gRPC error in device stream for %s: %s", device_id, err)
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AjaxAuthError("Session expired") from err
            raise AjaxApiError(f"gRPC error: {err}") from err

    async def async_find_notifications(
        self, space_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Find notifications for a space.

        Args:
            space_id: The space ID to get notifications for
            limit: Maximum number of notifications to retrieve (default 50)

        Returns:
            List of notification dictionaries with parsed data
        """
        if not self.session_token:
            raise AjaxAuthError("Not authenticated. Call async_login() first.")

        try:
            _LOGGER.debug("Fetching notifications for space_id: %s (limit: %d)", space_id, limit)

            # Create filter for the space
            from custom_components.ajax.systems.ajax.api.ecosystem.v2.communicationsvc.mobile.commonmodels.notification import (
                filter_pb2 as notification_filter_pb2,
                origin_id_pb2 as notification_origin_pb2,
            )

            # Create origin ID (space_id)
            origin = notification_origin_pb2.NotificationOriginId(
                space_id=space_id
            )

            notification_filter = notification_filter_pb2.NotificationsFilter(
                origin=origin
            )

            # Create request for finding notifications
            request = notification_find_pb2.FindNotificationsRequest(
                filter=notification_filter,
                limit=limit,
            )

            # Create stub and call service
            stub = notification_log_endpoints_pb2_grpc.NotificationLogServiceStub(self.channel)
            response = await stub.findNotifications(
                request, metadata=self._get_metadata(include_auth=True)
            )

            # Check response type
            if response.HasField("success"):
                notifications = []
                _LOGGER.info(
                    "Received %d notifications for space %s",
                    len(response.success.notifications),
                    space_id,
                )

                # Parse each notification
                for notification in response.success.notifications:
                    notification_data = self._parse_notification(notification)
                    if notification_data:
                        notifications.append(notification_data)

                return notifications

            elif response.HasField("failure"):
                failure = response.failure
                # Check if it's a "not_found" error (user doesn't have access to notifications)
                if hasattr(failure, "not_found") and failure.HasField("not_found"):
                    _LOGGER.info("User does not have access to notifications for space %s", space_id)
                    return []
                else:
                    _LOGGER.error("Failed to get notifications: %s", failure)
                    raise AjaxApiError("Failed to get notifications")
            else:
                _LOGGER.warning("Unknown response type received")
                return []

        except grpc.RpcError as err:
            _LOGGER.error("gRPC error getting notifications: %s", err)
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AjaxAuthError("Session expired") from err
            raise AjaxApiError(f"gRPC error: {err}") from err
        except (AjaxAuthError, AjaxApiError):
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error getting notifications")
            raise AjaxApiError(f"Failed to get notifications: {err}") from err

    async def async_stream_notification_updates(self, space_id: str):
        """Stream real-time notification updates for a space.

        Args:
            space_id: The space ID to stream notifications for

        Yields:
            Notification update events as they arrive
        """
        if not self.session_token:
            raise AjaxAuthError("Not authenticated. Call async_login() first.")

        try:
            _LOGGER.info("Starting notification streaming for space_id: %s", space_id)

            # Import necessary protobuf types
            from custom_components.ajax.systems.ajax.api.ecosystem.v2.communicationsvc.mobile.commonmodels.notification import (
                origin_id_pb2 as notification_origin_pb2,
            )

            # Create origin ID (space_id)
            origin = notification_origin_pb2.NotificationOriginId(
                space_id=space_id
            )

            # Create streaming request
            request = stream_notification_log_pb2.StreamNotificationLogRequest(
                origin=origin
            )

            # Create stub and start streaming
            stub = notification_log_endpoints_pb2_grpc.NotificationLogServiceStub(self.channel)

            async for response in stub.streamUpdates(
                request, metadata=self._get_metadata(include_auth=True)
            ):
                # Check response type
                if response.HasField("success"):
                    # Process events
                    for event in response.success.events:
                        yield event

                elif response.HasField("failure"):
                    _LOGGER.error("Notification stream failure: %s", response.failure)
                    raise AjaxApiError("Notification stream failed")

        except grpc.RpcError as err:
            # Handle authentication errors
            if err.code() == grpc.StatusCode.UNAUTHENTICATED:
                _LOGGER.error("Session expired for notification stream (space %s)", space_id)
                raise AjaxAuthError("Session expired") from err

            # Handle temporary connection errors
            elif err.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                # Don't log error here - coordinator will handle it based on retry count
                raise AjaxConnectionError("Connection lost to Ajax server") from err

            # Other gRPC errors (real problems)
            else:
                _LOGGER.error("gRPC error in notification stream for %s: %s (code: %s)", space_id, err, err.code())
                raise AjaxApiError(f"gRPC error: {err}") from err

        except (AjaxAuthError, AjaxApiError, AjaxConnectionError):
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error in notification streaming for space %s", space_id)
            raise AjaxApiError(f"Failed to stream notifications: {err}") from err

    def _parse_notification(self, notification) -> dict[str, Any] | None:
        """Parse a notification protobuf message to a dict.

        Args:
            notification: The notification protobuf message

        Returns:
            Dictionary with parsed notification data or None if parsing fails
        """
        try:
            notification_data = {
                "id": notification.id if hasattr(notification, "id") else None,
                "timestamp": None,
                "read": notification.read_by_user if hasattr(notification, "read_by_user") else False,
                "space_id": None,
                "device_id": None,
                "device_name": None,
                "room_id": None,
                "room_name": None,
                "event_type": None,
                "title": None,
                "message": None,
            }

            # Parse timestamp
            if hasattr(notification, "server_timestamp"):
                timestamp = notification.server_timestamp
                if timestamp:
                    notification_data["timestamp"] = timestamp.ToDatetime()

            # Parse space info
            if hasattr(notification, "space") and notification.space:
                space = notification.space
                if hasattr(space, "hex_id"):
                    notification_data["space_id"] = space.hex_id

            # Parse content - check if it's a hub notification (device events)
            if hasattr(notification, "content") and notification.content:
                content = notification.content

                # Hub notifications contain device events
                if content.HasField("hub_notification_content"):
                    hub_content = content.hub_notification_content

                    # Parse source (the device that triggered the event)
                    if hasattr(hub_content, "source") and hub_content.source:
                        source = hub_content.source
                        notification_data["device_id"] = source.id if hasattr(source, "id") else None
                        notification_data["device_name"] = source.name if hasattr(source, "name") else None
                        notification_data["room_id"] = source.room_hex_id if hasattr(source, "room_hex_id") else None
                        notification_data["room_name"] = source.room_name if hasattr(source, "room_name") else None

                    # Parse qualifier (event type)
                    if hasattr(hub_content, "qualifier") and hub_content.qualifier:
                        qualifier = hub_content.qualifier
                        if hasattr(qualifier, "tag") and qualifier.tag:
                            tag = qualifier.tag
                            # Get the event type from the oneof field
                            event_tag = tag.WhichOneof("event_tag_case")
                            if event_tag:
                                notification_data["event_type"] = event_tag
                                _LOGGER.debug(
                                    "Notification event: %s from device %s (%s)",
                                    event_tag,
                                    notification_data["device_name"],
                                    notification_data["device_id"],
                                )
                else:
                    # Handle space notifications (system events like arming/disarming)
                    content_type = content.WhichOneof("content")

                    if content_type == "space_notification_content":
                        space_content = content.space_notification_content

                        # Extract event type from qualifier.tag
                        if hasattr(space_content, "qualifier"):
                            qualifier = space_content.qualifier
                            if hasattr(qualifier, "tag"):
                                tag = qualifier.tag
                                # Check which tag field is set - the oneOf is in the tag itself
                                if hasattr(tag, "DESCRIPTOR"):
                                    # Find which field is set by checking all fields
                                    for field in tag.DESCRIPTOR.fields:
                                        if tag.HasField(field.name):
                                            tag_type = field.name

                                            # Map tag types to event types and messages
                                            tag_mapping = {
                                                "space_armed": ("armed", "Système armé"),
                                                "space_disarmed": ("disarmed", "Système désarmé"),
                                                "space_night_mode_on": ("night_mode_on", "Mode nuit activé"),
                                                "space_partially_armed": ("partially_armed", "Armement partiel activé"),
                                            }

                                            if tag_type in tag_mapping:
                                                notification_data["event_type"], notification_data["message"] = tag_mapping[tag_type]
                                            break

                        # Extract source information (who triggered the event)
                        if hasattr(space_content, "space_source"):
                            space_source = space_content.space_source
                            if hasattr(space_source, "name") and space_source.name:
                                source_name = space_source.name
                                # Append source to message if we have a message
                                if notification_data.get("message"):
                                    notification_data["message"] = f"{notification_data['message']} par {source_name}"

            return notification_data

        except Exception as err:
            _LOGGER.debug("Error parsing notification: %s", err, exc_info=True)
            return None

    def _parse_groups_from_space(self, space) -> dict[str, Any]:
        """Parse groups from a Space protobuf message.

        Args:
            space: The Space protobuf message from snapshot

        Returns:
            Dictionary with:
            - group_mode_enabled: bool - whether system uses group mode
            - night_mode_enabled: bool - whether night mode is enabled
            - groups: dict - group ID -> group data mapping
        """
        result = {
            "group_mode_enabled": False,
            "night_mode_enabled": False,
            "groups": {},
        }

        try:
            # Check if space has security field
            if not hasattr(space, "security") or not space.security:
                _LOGGER.debug("Space has no security field")
                return result

            security = space.security

            # Parse groups metadata (id, name, bulk arm/disarm settings)
            groups_metadata = {}
            if hasattr(security, "groups") and security.groups:
                _LOGGER.debug("Found %d groups in space security", len(security.groups))
                for group in security.groups:
                    group_id = group.id if hasattr(group, "id") else None
                    if not group_id:
                        continue

                    # Extract image ID if available
                    image_id = None
                    if hasattr(group, "images") and group.images:
                        images = group.images
                        if hasattr(images, "light") and images.light:
                            image_id = images.light if isinstance(images.light, str) else None

                    groups_metadata[group_id] = {
                        "id": group_id,
                        "name": group.name if hasattr(group, "name") else f"Group {group_id}",
                        "bulk_arm_involved": group.bulk_arm_involved if hasattr(group, "bulk_arm_involved") else False,
                        "bulk_disarm_involved": group.bulk_disarm_involved if hasattr(group, "bulk_disarm_involved") else False,
                        "image_id": image_id,
                    }

            # Check security mode
            if hasattr(security, "mode") and security.mode:
                mode = security.mode

                # Check if it's group mode
                if mode.HasField("group_mode"):
                    _LOGGER.debug("Space is in group mode")
                    result["group_mode_enabled"] = True
                    group_mode = mode.group_mode

                    # Parse night mode
                    if hasattr(group_mode, "night_mode_enabled"):
                        result["night_mode_enabled"] = group_mode.night_mode_enabled

                    # Parse group security states
                    if hasattr(group_mode, "groups") and group_mode.groups:
                        _LOGGER.debug("Found %d group security states", len(group_mode.groups))
                        for group_security in group_mode.groups:
                            group_id = group_security.group_id if hasattr(group_security, "group_id") else None
                            if not group_id:
                                continue

                            # Get metadata for this group
                            metadata = groups_metadata.get(group_id, {
                                "id": group_id,
                                "name": f"Group {group_id}",
                                "bulk_arm_involved": False,
                                "bulk_disarm_involved": False,
                                "image_id": None,
                            })

                            # Parse state
                            state = "none"
                            if hasattr(group_security, "state"):
                                state_value = group_security.state
                                # GroupSecurity.State enum values
                                if state_value == 1:  # GROUP_SECURITY_STATE_ARMED
                                    state = "armed"
                                elif state_value == 2:  # GROUP_SECURITY_STATE_DISARMED
                                    state = "disarmed"

                            # Parse night mode for this group
                            group_night_mode = False
                            if hasattr(group_security, "transition") and group_security.transition:
                                transition = group_security.transition
                                if hasattr(transition, "desired_state") and transition.desired_state:
                                    desired = transition.desired_state
                                    if hasattr(desired, "night_mode_enabled"):
                                        group_night_mode = desired.night_mode_enabled

                            result["groups"][group_id] = {
                                **metadata,
                                "state": state,
                                "night_mode_enabled": group_night_mode,
                            }

                elif mode.HasField("regular_mode"):
                    _LOGGER.debug("Space is in regular mode (not group mode)")
                    # In regular mode, there are no groups
                    # The overall space security state is handled separately

            _LOGGER.info(
                "Parsed %d groups, group_mode=%s, night_mode=%s",
                len(result["groups"]),
                result["group_mode_enabled"],
                result["night_mode_enabled"],
            )
            return result

        except Exception as err:
            _LOGGER.exception("Error parsing groups from space: %s", err)
            return result

    def _parse_rooms_from_space(self, space) -> dict[str, dict[str, Any]]:
        """Parse rooms from a Space protobuf message.

        Args:
            space: The Space protobuf message from snapshot

        Returns:
            Dictionary with room ID -> room data mapping
        """
        rooms = {}

        try:
            # Check if space has rooms field
            if not hasattr(space, "rooms"):
                return rooms

            if not space.rooms:
                _LOGGER.debug("Space has no rooms configured")
                return rooms

            for room in space.rooms:
                room_id = room.id if hasattr(room, "id") else None
                if not room_id:
                    continue

                room_name = room.name if hasattr(room, "name") else f"Room {room_id}"

                # Extract image ID if available
                image_id = None
                image_url = None
                if hasattr(room, "images") and room.images:
                    images = room.images
                    if hasattr(images, "light") and images.light:
                        image_id = images.light if isinstance(images.light, str) else None

                rooms[room_id] = {
                    "id": room_id,
                    "name": room_name,
                    "image_id": image_id,
                    "image_url": image_url,
                }

            _LOGGER.info("Parsed %d rooms from space", len(rooms))
            return rooms

        except Exception as err:
            _LOGGER.exception("Error parsing rooms from space: %s", err)
            return rooms

    async def async_turn_on_device(self, space_id: str, device_id: str, hub_id: str, channel_id: int = 1) -> None:
        """Turn on a device (socket/relay).

        Args:
            space_id: The space ID containing the device
            device_id: The device ID to control
            hub_id: The hub ID that the device is connected to
            channel_id: The channel number to turn on (default: 1)
        """
        if not self.session_token:
            raise AjaxAuthError("Not authenticated")

        try:
            _LOGGER.debug("Turning on device %s (channel %d)", device_id, channel_id)

            # Get device type - need to fetch device info
            # For now, assume socket type
            from custom_components.ajax.systems.ajax.api.ecosystem.v2.hubsvc.commonmodels import object_type_pb2

            # Create device type for socket
            device_type = object_type_pb2.ObjectType()
            device_type.socket.CopyFrom(object_type_pb2.ObjectType.Socket())

            # Map channel_id to Channel enum
            channel_enum = device_on_request_pb2.DeviceCommandDeviceOnRequest.CHANNEL_1
            if channel_id == 2:
                channel_enum = device_on_request_pb2.DeviceCommandDeviceOnRequest.CHANNEL_2
            elif channel_id == 3:
                channel_enum = device_on_request_pb2.DeviceCommandDeviceOnRequest.CHANNEL_3
            elif channel_id == 4:
                channel_enum = device_on_request_pb2.DeviceCommandDeviceOnRequest.CHANNEL_4

            # Create request
            request = device_on_request_pb2.DeviceCommandDeviceOnRequest(
                hub_id=hub_id,
                device_id=device_id,
                device_type=device_type,
                channels=[channel_enum],
            )

            # Create stub and call service
            stub = device_on_pb2_grpc.DeviceCommandDeviceOnServiceStub(self.channel)
            response = await stub.device_command_device_on(request, metadata=self._get_metadata(include_auth=True))

            # Check response
            if response.HasField("success"):
                _LOGGER.debug("Successfully turned on device %s", device_id)
            elif response.HasField("failure"):
                failure = response.failure
                error_msg = "Unknown error"

                if hasattr(failure, "hub_offline") and failure.HasField("hub_offline"):
                    error_msg = "Hub is offline"
                elif hasattr(failure, "device_offline") and failure.HasField("device_offline"):
                    error_msg = "Device is offline"
                elif hasattr(failure, "permission_denied") and failure.HasField("permission_denied"):
                    error_msg = "Permission denied"

                _LOGGER.error("Failed to turn on device: %s", error_msg)
                raise AjaxApiError(f"Failed to turn on device: {error_msg}")

        except grpc.RpcError as err:
            _LOGGER.exception("gRPC error turning on device: %s", err)
            raise AjaxApiError(f"Failed to turn on device: {err}")
        except Exception as err:
            _LOGGER.exception("Error turning on device: %s", err)
            raise AjaxApiError(f"Failed to turn on device: {err}")

    async def async_turn_off_device(self, space_id: str, device_id: str, hub_id: str, channel_id: int = 1) -> None:
        """Turn off a device (socket/relay).

        Args:
            space_id: The space ID containing the device
            device_id: The device ID to control
            hub_id: The hub ID that the device is connected to
            channel_id: The channel number to turn off (default: 1)
        """
        if not self.session_token:
            raise AjaxAuthError("Not authenticated")

        try:
            _LOGGER.debug("Turning off device %s (channel %d)", device_id, channel_id)

            # Get device type - need to fetch device info
            # For now, assume socket type
            from custom_components.ajax.systems.ajax.api.ecosystem.v2.hubsvc.commonmodels import object_type_pb2

            # Create device type for socket
            device_type = object_type_pb2.ObjectType()
            device_type.socket.CopyFrom(object_type_pb2.ObjectType.Socket())

            # Map channel_id to Channel enum
            channel_enum = device_off_request_pb2.DeviceCommandDeviceOffRequest.CHANNEL_1
            if channel_id == 2:
                channel_enum = device_off_request_pb2.DeviceCommandDeviceOffRequest.CHANNEL_2
            elif channel_id == 3:
                channel_enum = device_off_request_pb2.DeviceCommandDeviceOffRequest.CHANNEL_3
            elif channel_id == 4:
                channel_enum = device_off_request_pb2.DeviceCommandDeviceOffRequest.CHANNEL_4

            # Create request
            request = device_off_request_pb2.DeviceCommandDeviceOffRequest(
                hub_id=hub_id,
                device_id=device_id,
                device_type=device_type,
                channels=[channel_enum],
            )

            # Create stub and call service
            stub = device_off_pb2_grpc.DeviceCommandDeviceOffServiceStub(self.channel)
            response = await stub.device_command_device_off(request, metadata=self._get_metadata(include_auth=True))

            # Check response
            if response.HasField("success"):
                _LOGGER.debug("Successfully turned off device %s", device_id)
            elif response.HasField("failure"):
                failure = response.failure
                error_msg = "Unknown error"

                if hasattr(failure, "hub_offline") and failure.HasField("hub_offline"):
                    error_msg = "Hub is offline"
                elif hasattr(failure, "device_offline") and failure.HasField("device_offline"):
                    error_msg = "Device is offline"
                elif hasattr(failure, "permission_denied") and failure.HasField("permission_denied"):
                    error_msg = "Permission denied"

                _LOGGER.error("Failed to turn off device: %s", error_msg)
                raise AjaxApiError(f"Failed to turn off device: {error_msg}")

        except grpc.RpcError as err:
            _LOGGER.exception("gRPC error turning off device: %s", err)
            raise AjaxApiError(f"Failed to turn off device: {err}")
        except Exception as err:
            _LOGGER.exception("Error turning off device: %s", err)
            raise AjaxApiError(f"Failed to turn off device: {err}")

    async def close(self) -> None:
        """Close the gRPC channel."""
        if self.channel:
            await self.channel.close()
