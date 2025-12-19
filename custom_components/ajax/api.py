"""Ajax REST API client based on official PDF documentation."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import aiohttp

from .const import (
    AJAX_REST_API_BASE_URL,
    AJAX_REST_API_TIMEOUT,
    AUTH_MODE_DIRECT,
    AUTH_MODE_PROXY_SECURE,
)

_LOGGER = logging.getLogger(__name__)


class AjaxRestApiError(Exception):
    """Base exception for Ajax REST API errors."""


class AjaxRestAuthError(AjaxRestApiError):
    """Authentication error."""


class AjaxRest2FARequiredError(AjaxRestApiError):
    """2FA is required."""

    def __init__(self, request_id: str):
        """Initialize 2FA error with request ID."""
        super().__init__("Two-factor authentication required")
        self.request_id = request_id


class AjaxRestApi:
    """Ajax REST API client.

    Authentication as User (from PDF page 4-5):
    - First login via Credentials: E-mail + SHA256(password)
    - Generate a temporary token
    - Use this token for subsequent API calls

    Supports three authentication modes:
    - Direct: Connect directly to Ajax API with API key
    - Proxy Secure: All requests go through proxy (proxy adds API key)
    - Proxy Hybrid: Login via proxy to get API key, then direct API calls
    """

    def __init__(
        self,
        api_key: str,
        email: str,
        password: str,
        password_is_hashed: bool = False,
        proxy_url: str | None = None,
        proxy_mode: str | None = None,
    ):
        """Initialize the API client.

        Args:
            api_key: API Key provided by Ajax Systems (can be empty for proxy modes)
            email: User email address
            password: User password (plain or SHA256 hashed)
            password_is_hashed: True if password is already SHA256 hashed
            proxy_url: URL of proxy server (for proxy modes)
            proxy_mode: Authentication mode (direct, proxy_secure, proxy_hybrid)
        """
        self.api_key = api_key
        self.email = email
        self.proxy_url = proxy_url.rstrip("/") if proxy_url else None
        self.proxy_mode = proxy_mode or AUTH_MODE_DIRECT
        self.sse_url: str | None = None  # SSE endpoint URL (set by proxy on login)

        # Hash password if not already hashed
        if password_is_hashed:
            self.password_hash = password
        else:
            self.password_hash = hashlib.sha256(password.encode()).hexdigest()

        self.session: aiohttp.ClientSession | None = None
        self.session_token: str | None = None  # Session token (15 min TTL)
        self.refresh_token: str | None = None  # Refresh token (7 days TTL)
        self.user_id: str | None = None  # User ID from login

        # Base headers with API key (may be empty for proxy modes initially)
        self._base_headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            self._base_headers["X-Api-Key"] = api_key

    @property
    def is_proxy_mode(self) -> bool:
        """Check if using proxy mode (vs direct API)."""
        return self.proxy_mode != AUTH_MODE_DIRECT or self.proxy_url is not None

    def _get_base_url(self, for_login: bool = False) -> str:
        """Get the base URL based on auth mode.

        Args:
            for_login: True if this is for login request

        Returns:
            Base URL to use for API requests
        """
        if self.proxy_mode == AUTH_MODE_PROXY_SECURE:
            # Secure mode: ALL requests go through proxy
            return f"{self.proxy_url}/api" if self.proxy_url else AJAX_REST_API_BASE_URL
        elif self.proxy_mode and self.proxy_url and for_login:
            # Hybrid mode: only login goes through proxy
            return self.proxy_url
        else:
            # Direct mode or hybrid mode after login
            return AJAX_REST_API_BASE_URL

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self):
        """Close the session."""
        if self.session and not self.session.closed:
            await self.session.close()

    async def async_login(self) -> str:
        """Login with email and SHA256(password) to get session token.

        According to Swagger API 1.130.0:
        - Authenticates with email + SHA256(password)
        - Returns sessionToken (15 min TTL), refreshToken (7 days TTL), and userId
        - POST body: {"login": email, "passwordHash": hash}

        For proxy modes, the proxy may also return:
        - apiKey: API key to use for direct requests (hybrid mode)
        - sseUrl: URL for SSE event stream

        Returns:
            Session token string

        Raises:
            AjaxRest2FARequiredError: If 2FA is required
            AjaxRestAuthError: If authentication fails
            AjaxRestApiError: For other API errors
        """
        _LOGGER.debug(
            "Logging in with email: %s (mode: %s)", self.email, self.proxy_mode
        )

        session = await self._get_session()
        base_url = self._get_base_url(for_login=True)
        url = f"{base_url}/login"

        # Login request body according to Swagger
        payload = {
            "login": self.email,
            "passwordHash": self.password_hash,
        }

        try:
            async with session.post(
                url,
                headers=self._base_headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=AJAX_REST_API_TIMEOUT),
            ) as response:
                _LOGGER.debug("Login response status: %s", response.status)

                if response.status == 401:
                    raise AjaxRestAuthError("Invalid email or password")
                elif response.status == 403:
                    # 2FA required
                    result = await response.json()
                    request_id = result.get("requestId", "")
                    _LOGGER.info("2FA required, request_id: %s", request_id)
                    raise AjaxRest2FARequiredError(request_id)

                response.raise_for_status()
                result = await response.json()

                # Extract tokens from response
                # Proxy format: {"user_id": "xxx"}
                # Direct API format: {"sessionToken": "xxx", "userId": "xxx"}
                self.session_token = result.get("sessionToken")
                self.refresh_token = result.get("refreshToken")
                self.user_id = result.get("userId") or result.get("user_id")

                # For proxy modes, extract additional info
                if self.proxy_url:
                    # Proxy returns user_id, we use it as session token for SSE
                    if not self.session_token and self.user_id:
                        self.session_token = self.user_id
                        _LOGGER.debug("Using user_id as session token for proxy mode")

                    # API key provided by proxy (for hybrid mode)
                    proxy_api_key = result.get("apiKey")
                    if proxy_api_key:
                        self.api_key = proxy_api_key
                        self._base_headers["X-Api-Key"] = proxy_api_key
                        _LOGGER.info("Received API key from proxy")

                    # SSE URL for real-time events
                    self.sse_url = result.get("sseUrl")
                    if not self.sse_url and self.proxy_url and self.user_id:
                        # Build SSE URL from proxy URL if not provided
                        self.sse_url = f"{self.proxy_url}/events?userId={self.user_id}"
                    if self.sse_url:
                        _LOGGER.info("SSE endpoint: %s", self.sse_url)

                if not self.session_token:
                    raise AjaxRestApiError("No sessionToken in login response")

                _LOGGER.info(
                    "Login successful, session token obtained (userId: %s)",
                    self.user_id,
                )
                return self.session_token

        except aiohttp.ClientError as err:
            _LOGGER.error("Login request failed: %s", err)
            raise AjaxRestApiError(f"Login failed: {err}") from err
        except asyncio.TimeoutError as err:
            _LOGGER.error("Login request timeout")
            raise AjaxRestApiError("Login timeout") from err

    async def async_verify_2fa(self, request_id: str, code: str) -> str:
        """Verify 2FA code and get temporary token.

        Args:
            request_id: Request ID from login response
            code: 6-digit 2FA code

        Returns:
            Temporary token string

        Raises:
            AjaxRestAuthError: If 2FA verification fails
            AjaxRestApiError: For other API errors
        """
        _LOGGER.debug("Verifying 2FA code")

        session = await self._get_session()
        base_url = self._get_base_url(for_login=True)
        url = f"{base_url}/login/2fa"

        headers = {
            **self._base_headers,
            "X-Request-Id": request_id,
            "X-2FA-Code": code,
        }

        try:
            async with session.post(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=AJAX_REST_API_TIMEOUT),
            ) as response:
                if response.status == 401:
                    raise AjaxRestAuthError("Invalid 2FA code")

                response.raise_for_status()
                result = await response.json()

                # 2FA returns sessionToken like normal login
                self.session_token = result.get("sessionToken")
                if not self.session_token:
                    raise AjaxRestApiError("No sessionToken in 2FA response")

                _LOGGER.info("2FA verification successful")
                return self.session_token

        except aiohttp.ClientError as err:
            _LOGGER.error("2FA verification failed: %s", err)
            raise AjaxRestApiError(f"2FA verification failed: {err}") from err
        except asyncio.TimeoutError as err:
            _LOGGER.error("2FA verification timeout")
            raise AjaxRestApiError("2FA verification timeout") from err

    async def async_refresh_token(self) -> str:
        """Refresh session token using refresh token.

        According to Swagger API 1.130.0:
        - Uses refreshToken to get a new sessionToken without re-authenticating
        - Extends session without requiring email/password
        - POST body: {"refreshToken": refresh_token, "userId": user_id}

        Returns:
            New session token string

        Raises:
            AjaxRestAuthError: If refresh token is invalid or expired
            AjaxRestApiError: For other API errors
        """
        if not self.refresh_token or not self.user_id:
            raise AjaxRestApiError(
                "No refresh token available. Call async_login() first."
            )

        _LOGGER.debug("Refreshing session token for user: %s", self.user_id)

        session = await self._get_session()
        url = f"{self._get_base_url()}/refresh"

        # Refresh request body according to Swagger
        payload = {
            "refreshToken": self.refresh_token,
            "userId": self.user_id,
        }

        try:
            async with session.post(
                url,
                headers=self._base_headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=AJAX_REST_API_TIMEOUT),
            ) as response:
                _LOGGER.debug("Refresh response status: %s", response.status)

                if response.status == 401:
                    raise AjaxRestAuthError("Refresh token expired or invalid")

                response.raise_for_status()
                result = await response.json()

                # Extract new tokens from response
                self.session_token = result.get("sessionToken")
                self.refresh_token = result.get("refreshToken")
                # userId should remain the same

                if not self.session_token:
                    raise AjaxRestApiError("No sessionToken in refresh response")

                _LOGGER.info(
                    "Session token refreshed successfully (userId: %s)", self.user_id
                )
                return self.session_token

        except aiohttp.ClientError as err:
            _LOGGER.error("Token refresh request failed: %s", err)
            raise AjaxRestApiError(f"Token refresh failed: {err}") from err
        except asyncio.TimeoutError as err:
            _LOGGER.error("Token refresh request timeout")
            raise AjaxRestApiError("Token refresh timeout") from err

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        _retry_on_auth_error: bool = True,
    ) -> Any:
        """Make API request with session token.

        Automatically renews the token if it expires (401 error).

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (without base URL)
            data: Optional JSON data for POST/PUT requests
            _retry_on_auth_error: Internal flag to prevent infinite retry loop

        Returns:
            API response as dict

        Raises:
            AjaxRestAuthError: If authentication fails
            AjaxRestApiError: For other API errors
        """
        if not self.session_token:
            raise AjaxRestApiError("Not logged in. Call async_login() first.")

        url = f"{self._get_base_url()}/{endpoint}"
        session = await self._get_session()

        # Headers with session token (Swagger spec)
        headers = {
            **self._base_headers,
            "X-Session-Token": self.session_token,
        }

        try:
            async with session.request(
                method,
                url,
                headers=headers,
                json=data,
                timeout=aiohttp.ClientTimeout(total=AJAX_REST_API_TIMEOUT),
            ) as response:
                if response.status == 401:
                    if _retry_on_auth_error:
                        # Token expired, try to refresh it first
                        try:
                            # Try refresh token first (faster, no password needed)
                            await self.async_refresh_token()
                            _LOGGER.info(
                                "Token refreshed successfully, retrying request"
                            )
                            # Retry the request with the new token (only once)
                            return await self._request(
                                method, endpoint, data, _retry_on_auth_error=False
                            )
                        except AjaxRestAuthError:
                            # Refresh token expired or invalid, fallback to full login
                            _LOGGER.warning(
                                "Refresh token failed, falling back to full login"
                            )
                            try:
                                await self.async_login()
                                _LOGGER.info("Full login successful, retrying request")
                                return await self._request(
                                    method, endpoint, data, _retry_on_auth_error=False
                                )
                            except Exception as err:
                                _LOGGER.error("Failed to renew token: %s", err)
                                raise AjaxRestAuthError("Token renewal failed") from err
                        except Exception as err:
                            _LOGGER.error("Token refresh failed: %s", err)
                            raise AjaxRestAuthError("Token refresh failed") from err
                    else:
                        # Already retried once, give up
                        _LOGGER.error("Authentication failed after token renewal")
                        raise AjaxRestAuthError("Invalid or expired token")
                elif response.status == 403:
                    _LOGGER.error("Access denied (403) - Insufficient permissions")
                    raise AjaxRestAuthError("Access denied")

                response.raise_for_status()
                return await response.json()

        except aiohttp.ClientError as err:
            _LOGGER.error("API request to %s failed: %s", endpoint, err)
            raise AjaxRestApiError(f"API request failed: {err}") from err
        except asyncio.TimeoutError as err:
            _LOGGER.error(
                "API request to %s timed out after %ss", endpoint, AJAX_REST_API_TIMEOUT
            )
            raise AjaxRestApiError("API request timeout") from err

    # Hub methods
    async def async_get_hubs(self) -> list[dict[str, Any]]:
        """Get all hubs.

        Returns:
            List of hub dictionaries
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        return await self._request("GET", f"user/{self.user_id}/hubs")

    async def async_get_hub(self, hub_id: str) -> dict[str, Any]:
        """Get hub details.

        Args:
            hub_id: Hub ID

        Returns:
            Hub details dictionary
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        return await self._request("GET", f"user/{self.user_id}/hubs/{hub_id}")

    async def async_get_rooms(self, hub_id: str) -> list[dict[str, Any]]:
        """Get rooms for a hub.

        Args:
            hub_id: Hub ID

        Returns:
            List of room dictionaries
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        return await self._request("GET", f"user/{self.user_id}/hubs/{hub_id}/rooms")

    async def async_get_users(self, hub_id: str) -> list[dict[str, Any]]:
        """Get users for a hub.

        Args:
            hub_id: Hub ID

        Returns:
            List of user dictionaries
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        return await self._request("GET", f"user/{self.user_id}/hubs/{hub_id}/users")

    async def async_get_hub_mode(self, hub_id: str) -> dict[str, Any]:
        """Get hub alarm mode.

        Args:
            hub_id: Hub ID

        Returns:
            Hub mode dictionary
        """
        return await self._request("GET", f"hubs/{hub_id}/mode")

    async def async_set_hub_mode(self, hub_id: str, mode: str) -> dict[str, Any]:
        """Set hub alarm mode.

        Args:
            hub_id: Hub ID
            mode: Alarm mode (full, partial_1, night, disarmed)

        Returns:
            Updated hub mode
        """
        return await self._request("POST", f"hubs/{hub_id}/mode", {"mode": mode})

    # Device methods
    async def async_get_devices(
        self, hub_id: str, enrich: bool = True
    ) -> list[dict[str, Any]]:
        """Get all devices for a specific hub.

        Args:
            hub_id: Hub ID
            enrich: If True, returns full device details (default True)

        Returns:
            List of device dictionaries (with full details if enrich=True)
        """
        endpoint = f"user/{self.user_id}/hubs/{hub_id}/devices"
        if enrich:
            endpoint += "?enrich=true"
        return await self._request("GET", endpoint)

    async def async_get_device(self, hub_id: str, device_id: str) -> dict[str, Any]:
        """Get device details.

        Args:
            hub_id: Hub ID
            device_id: Device ID

        Returns:
            Device details dictionary
        """
        return await self._request(
            "GET", f"user/{self.user_id}/hubs/{hub_id}/devices/{device_id}"
        )

    async def async_get_device_state(self, device_id: str) -> dict[str, Any]:
        """Get device state.

        Args:
            device_id: Device ID

        Returns:
            Device state dictionary
        """
        return await self._request("GET", f"devices/{device_id}/state")

    # Device control methods (direct API mode)
    async def async_control_device(
        self, device_id: str, command: dict[str, Any]
    ) -> dict[str, Any]:
        """Send control command to device (direct API mode only).

        This endpoint is used for Socket/Relay/WallSwitch control in direct mode.
        For proxy mode, use async_update_device with switchState instead.

        Args:
            device_id: Device ID
            command: Command dictionary (e.g., {"state": "on"} or {"action": "pulse"})

        Returns:
            Device response
        """
        return await self._request("POST", f"devices/{device_id}/control", command)

    async def async_send_device_command(
        self, hub_id: str, device_id: str, command: str, device_type: str
    ) -> None:
        """Send command to device (Socket/Relay/WallSwitch).

        Uses the /command endpoint which is simpler and more reliable than PUT.
        Supported commands: SWITCH_ON, SWITCH_OFF

        Args:
            hub_id: Hub ID
            device_id: Device ID
            command: Command string (e.g., "SWITCH_ON", "SWITCH_OFF")
            device_type: Device type string (e.g., "WALL_SWITCH", "SOCKET", "RELAY")
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        endpoint = f"user/{self.user_id}/hubs/{hub_id}/devices/{device_id}/command"
        payload = {"command": command, "deviceType": device_type}
        _LOGGER.info(
            "Sending device command: POST %s with %s",
            endpoint,
            payload,
        )
        await self._request_no_response("POST", endpoint, payload)

    async def async_set_switch_state(
        self, hub_id: str, device_id: str, state: bool, device_type: str
    ) -> None:
        """Set switch/relay/socket state.

        Uses the /command endpoint for reliable switch control.

        Args:
            hub_id: Hub ID
            device_id: Device ID
            state: True for on, False for off
            device_type: Device type string (e.g., "WallSwitch", "Socket", "Relay")
        """
        command = "SWITCH_ON" if state else "SWITCH_OFF"
        await self.async_send_device_command(hub_id, device_id, command, device_type)

    # Socket methods
    async def async_get_socket_power(self, device_id: str) -> dict[str, Any]:
        """Get socket power consumption.

        Args:
            device_id: Socket device ID

        Returns:
            Power consumption data
        """
        return await self._request("GET", f"devices/{device_id}/power")

    # Camera methods
    async def async_get_cameras(self) -> list[dict[str, Any]]:
        """Get all cameras.

        Returns:
            List of camera dictionaries
        """
        return await self._request("GET", "cameras")

    async def async_get_camera(self, camera_id: str) -> dict[str, Any]:
        """Get camera details.

        Args:
            camera_id: Camera ID

        Returns:
            Camera details dictionary
        """
        return await self._request("GET", f"cameras/{camera_id}")

    async def async_get_camera_snapshot(self, camera_id: str) -> bytes:
        """Get camera snapshot.

        Args:
            camera_id: Camera ID

        Returns:
            Snapshot image data as bytes
        """
        url = f"{self._get_base_url()}/cameras/{camera_id}/snapshot"
        session = await self._get_session()

        headers = {
            **self._base_headers,
            "X-Session-Token": self.session_token,
        }

        async with session.get(url, headers=headers) as response:
            response.raise_for_status()
            return await response.read()

    async def async_get_camera_stream_url(self, camera_id: str) -> str:
        """Get camera stream URL.

        Args:
            camera_id: Camera ID

        Returns:
            Stream URL string
        """
        data = await self._request("GET", f"cameras/{camera_id}/stream")
        return data.get("url", "")

    # Light/Button methods
    async def async_set_light_state(
        self,
        device_id: str,
        state: bool,
        brightness: int | None = None,
    ) -> dict[str, Any]:
        """Set light state.

        Args:
            device_id: Light device ID
            state: True for on, False for off
            brightness: Optional brightness (0-100)

        Returns:
            Updated light state
        """
        payload = {"state": "on" if state else "off"}
        if brightness is not None:
            payload["brightness"] = brightness
        return await self._request("POST", f"devices/{device_id}/control", payload)

    # Automation methods
    async def async_get_automations(self, hub_id: str) -> list[dict[str, Any]]:
        """Get hub automations/scenarios.

        Args:
            hub_id: Hub ID

        Returns:
            List of automation dictionaries
        """
        return await self._request("GET", f"hubs/{hub_id}/automations")

    async def async_trigger_automation(
        self,
        hub_id: str,
        automation_id: str,
    ) -> dict[str, Any]:
        """Trigger automation/scenario.

        Args:
            hub_id: Hub ID
            automation_id: Automation ID

        Returns:
            Automation trigger response
        """
        return await self._request(
            "POST", f"hubs/{hub_id}/automations/{automation_id}/trigger"
        )

    # Events methods
    async def async_get_events(
        self, hub_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Get hub events history.

        Args:
            hub_id: Hub ID
            limit: Maximum number of events to return

        Returns:
            List of event dictionaries
        """
        return await self._request("GET", f"hubs/{hub_id}/events?limit={limit}")

    # NVR methods
    async def async_get_nvr_status(self, nvr_id: str) -> dict[str, Any]:
        """Get NVR status.

        Args:
            nvr_id: NVR device ID

        Returns:
            NVR status dictionary
        """
        return await self._request("GET", f"devices/{nvr_id}/status")

    async def async_get_nvr_recordings(
        self,
        nvr_id: str,
        camera_id: str,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        """Get NVR recordings for a camera.

        Args:
            nvr_id: NVR device ID
            camera_id: Camera ID
            start: Start time (ISO format)
            end: End time (ISO format)

        Returns:
            List of recording dictionaries
        """
        return await self._request(
            "GET",
            f"devices/{nvr_id}/recordings?cameraId={camera_id}&start={start}&end={end}",
        )

    # Device settings methods
    async def async_update_device(
        self,
        hub_id: str,
        device_id: str,
        settings: dict[str, Any],
    ) -> None:
        """Update device settings.

        Args:
            hub_id: Hub ID
            device_id: Device ID
            settings: Dictionary of settings to update (e.g., {"alwaysActive": true})

        Raises:
            AjaxRestApiError: If the update fails
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        # First get current device data
        current_device = await self.async_get_device(hub_id, device_id)

        # Merge settings with current device data
        updated_device = {**current_device, **settings}

        await self._request_no_response(
            "PUT",
            f"user/{self.user_id}/hubs/{hub_id}/devices/{device_id}",
            updated_device,
        )

    async def _request_no_response(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        _retry_on_auth_error: bool = True,
    ) -> None:
        """Make API request that returns no content (204).

        Args:
            method: HTTP method (PUT, DELETE, etc.)
            endpoint: API endpoint (without base URL)
            data: Optional JSON data for request body
            _retry_on_auth_error: Internal flag to prevent infinite retry loop

        Raises:
            AjaxRestAuthError: If authentication fails
            AjaxRestApiError: For other API errors
        """
        if not self.session_token:
            raise AjaxRestApiError("Not logged in. Call async_login() first.")

        url = f"{self._get_base_url()}/{endpoint}"
        session = await self._get_session()

        headers = {
            **self._base_headers,
            "X-Session-Token": self.session_token,
        }

        try:
            async with session.request(
                method,
                url,
                headers=headers,
                json=data,
                timeout=aiohttp.ClientTimeout(total=AJAX_REST_API_TIMEOUT),
            ) as response:
                if response.status == 401:
                    if _retry_on_auth_error:
                        try:
                            await self.async_refresh_token()
                            return await self._request_no_response(
                                method, endpoint, data, _retry_on_auth_error=False
                            )
                        except AjaxRestAuthError:
                            await self.async_login()
                            return await self._request_no_response(
                                method, endpoint, data, _retry_on_auth_error=False
                            )
                    else:
                        raise AjaxRestAuthError("Invalid or expired token")

                if response.status not in (200, 202, 204):
                    error_text = await response.text()
                    _LOGGER.error("API error %s: %s", response.status, error_text)
                    raise AjaxRestApiError(f"API error {response.status}: {error_text}")

        except aiohttp.ClientError as err:
            _LOGGER.error("API request to %s failed: %s", endpoint, err)
            raise AjaxRestApiError(f"API request failed: {err}") from err
        except asyncio.TimeoutError as err:
            _LOGGER.error("API request to %s timed out", endpoint)
            raise AjaxRestApiError("API request timeout") from err

    # Arming commands
    async def async_arm(self, hub_id: str, ignore_problems: bool = True) -> None:
        """Arm the hub.

        Args:
            hub_id: Hub ID
            ignore_problems: Whether to ignore sensor problems when arming
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        await self._request_no_response(
            "PUT",
            f"user/{self.user_id}/hubs/{hub_id}/commands/arming",
            {"command": "ARM", "ignoreProblems": ignore_problems},
        )

    async def async_disarm(self, hub_id: str, ignore_problems: bool = True) -> None:
        """Disarm the hub.

        Args:
            hub_id: Hub ID
            ignore_problems: Whether to ignore sensor problems
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        await self._request_no_response(
            "PUT",
            f"user/{self.user_id}/hubs/{hub_id}/commands/arming",
            {"command": "DISARM", "ignoreProblems": ignore_problems},
        )

    async def async_night_mode(self, hub_id: str, enabled: bool = True) -> None:
        """Set night mode on/off.

        Args:
            hub_id: Hub ID
            enabled: True for night mode on, False for off
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")

        command = "NIGHT_MODE_ON" if enabled else "NIGHT_MODE_OFF"
        await self._request_no_response(
            "PUT",
            f"user/{self.user_id}/hubs/{hub_id}/commands/arming",
            {"command": command, "ignoreProblems": True},
        )
