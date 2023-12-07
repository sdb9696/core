"""Config flow for TP-Link."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Optional

from kasa import AuthenticationException, Credentials, SmartDevice, SmartDeviceException
from kasa.discover import Discover
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import dhcp
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigEntryState
from homeassistant.const import (
    CONF_ALIAS,
    CONF_DEVICE,
    CONF_HOST,
    CONF_MAC,
    CONF_MODEL,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import DiscoveryInfoType

from . import async_discover_devices, get_stored_credentials, set_stored_credentials
from .const import CONF_CONNECTION_PARAMS, CONF_DEVICE_TYPE, DOMAIN

STEP_AUTH_DATA_SCHEMA = vol.Schema(
    {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for tplink."""

    VERSION = 1
    reauth_entry: ConfigEntry | None = None

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_devices: dict[str, SmartDevice] = {}
        self._discovered_device: SmartDevice | None = None
        self._last_failed_discovery_credentials: Optional[Credentials] = None

    async def async_step_dhcp(self, discovery_info: dhcp.DhcpServiceInfo) -> FlowResult:
        """Handle discovery via dhcp."""
        return await self._async_handle_discovery(
            discovery_info.ip, discovery_info.macaddress
        )

    async def async_step_integration_discovery(
        self, discovery_info: DiscoveryInfoType
    ) -> FlowResult:
        """Handle integration discovery."""
        return await self._async_handle_discovery(
            discovery_info[CONF_HOST], discovery_info[CONF_MAC]
        )

    async def _async_handle_discovery(self, host: str, mac: str) -> FlowResult:
        """Handle any discovery."""
        await self.async_set_unique_id(dr.format_mac(mac))
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        self._async_abort_entries_match({CONF_HOST: host})
        self.context[CONF_HOST] = host
        for progress in self._async_in_progress():
            if progress.get("context", {}).get(CONF_HOST) == host:
                return self.async_abort(reason="already_in_progress")
        credentials = await get_stored_credentials(self.hass)
        try:
            (
                self._discovered_device,
                connect_error,
            ) = await self._async_try_discover_connect(
                host, credentials, raise_on_progress=True
            )
            if connect_error:
                raise connect_error
        except AuthenticationException:
            self._last_failed_discovery_credentials = credentials
            return await self.async_step_discovery_auth_confirm()
        except SmartDeviceException:
            return self.async_abort(reason="cannot_connect")

        return await self.async_step_discovery_confirm()

    async def async_step_discovery_auth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Dialog that informs the user that auth is required."""
        assert self._discovered_device is not None
        errors = {}
        host = self.context[CONF_HOST]
        credentials = await get_stored_credentials(self.hass)

        if credentials != self._last_failed_discovery_credentials:
            try:
                device = await SmartDevice.connect(
                    host,
                    port=self._discovered_device.port,
                    credentials=credentials,
                    connection_params=self._discovered_device.connection_parameters,
                    device_type=self._discovered_device.device_type,
                    try_discovery_on_error=False,
                )
            except AuthenticationException:
                pass
            else:
                self._discovered_device = device
                return await self.async_step_discovery_confirm()

        if user_input:
            credentials = Credentials(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            try:
                device = await SmartDevice.connect(
                    host,
                    port=self._discovered_device.port,
                    credentials=credentials,
                    connection_params=self._discovered_device.connection_parameters,
                    device_type=self._discovered_device.device_type,
                    try_discovery_on_error=False,
                )
            except AuthenticationException:
                errors["base"] = "invalid_auth"
            except SmartDeviceException:
                return self.async_abort(reason="cannot_connect")
            else:
                self._discovered_device = device
                await set_stored_credentials(
                    self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
                async_call_later(
                    self.hass, 0.5, self._async_reload_requires_auth_entries
                )
                return await self.async_step_discovery_confirm()

        placeholders = {
            "name": self._discovered_device.alias,
            "model": self._discovered_device.model,
            "host": self._discovered_device.host,
        }
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="discovery_auth_confirm",
            data_schema=STEP_AUTH_DATA_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm discovery."""
        assert self._discovered_device is not None
        if user_input is not None:
            return self._async_create_entry_from_device(self._discovered_device)

        self._set_confirm_only()
        placeholders = {
            "name": self._discovered_device.alias,
            "model": self._discovered_device.model,
            "host": self._discovered_device.host,
        }
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="discovery_confirm", description_placeholders=placeholders
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            if not (host := user_input[CONF_HOST]):
                return await self.async_step_pick_device()
            self.context[CONF_HOST] = host
            credentials = await get_stored_credentials(self.hass)
            try:
                device, connect_error = await self._async_try_discover_connect(
                    host, credentials, raise_on_progress=False
                )
                if connect_error:
                    raise connect_error
            except AuthenticationException:
                return await self.async_step_user_auth_confirm()
            except SmartDeviceException:
                errors["base"] = "cannot_connect"
            else:
                return self._async_create_entry_from_device(device)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Optional(CONF_HOST, default=""): str}),
            errors=errors,
        )

    async def async_step_user_auth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Dialog that informs the user that auth is required."""
        errors = {}
        host = self.context[CONF_HOST]
        if user_input:
            credentials = Credentials(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            try:
                device, connect_error = await self._async_try_discover_connect(
                    host, credentials, raise_on_progress=False
                )
                if connect_error:
                    raise connect_error
            except AuthenticationException:
                errors["base"] = "invalid_auth"
            except SmartDeviceException:
                errors["base"] = "cannot_connect"
            else:
                await set_stored_credentials(
                    self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
                async_call_later(
                    self.hass, 0.5, self._async_reload_requires_auth_entries
                )
                return self._async_create_entry_from_device(device)

        return self.async_show_form(
            step_id="user_auth_confirm",
            data_schema=STEP_AUTH_DATA_SCHEMA,
            errors=errors,
            description_placeholders={CONF_HOST: host},
        )

    async def async_step_pick_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the step to pick discovered device."""
        if user_input is not None:
            mac = user_input[CONF_DEVICE]
            await self.async_set_unique_id(mac, raise_on_progress=False)
            self._discovered_device = self._discovered_devices[mac]
            try:
                await self._discovered_device.update()
            except AuthenticationException:
                return await self.async_step_pick_device_auth_confirm()
            except SmartDeviceException:
                return self.async_abort(reason="cannot_connect")
            return self._async_create_entry_from_device(self._discovered_device)

        configured_devices = {
            entry.unique_id for entry in self._async_current_entries()
        }
        self._discovered_devices = await async_discover_devices(self.hass)
        devices_name = {
            formatted_mac: (
                f"{device.alias} {device.model} ({device.host}) {formatted_mac}"
            )
            for formatted_mac, device in self._discovered_devices.items()
            if formatted_mac not in configured_devices
        }
        # Check if there is at least one device
        if not devices_name:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="pick_device",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE): vol.In(devices_name)}),
        )

    async def async_step_pick_device_auth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Dialog that informs the user that auth is required."""
        errors = {}
        assert self._discovered_device is not None
        host = self._discovered_device.host
        if user_input:
            credentials = Credentials(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            try:
                device = await SmartDevice.connect(
                    host,
                    port=self._discovered_device.port,
                    credentials=credentials,
                    connection_params=self._discovered_device.connection_parameters,
                    device_type=self._discovered_device.device_type,
                    try_discovery_on_error=False,
                )
            except AuthenticationException:
                errors["base"] = "invalid_auth"
            except SmartDeviceException:
                errors["base"] = "cannot_connect"
            else:
                await set_stored_credentials(
                    self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
                async_call_later(
                    self.hass, 0.5, self._async_reload_requires_auth_entries
                )
                return self._async_create_entry_from_device(device)

        return self.async_show_form(
            step_id="pick_device_auth_confirm",
            data_schema=STEP_AUTH_DATA_SCHEMA,
            errors=errors,
            description_placeholders={CONF_HOST: host},
        )

    async def _async_reload_requires_auth_entries(self, _now: datetime) -> None:
        entries: list[ConfigEntry] = self._async_current_entries(include_ignore=False)
        for entry in entries:
            if self.reauth_entry and entry.entry_id == self.reauth_entry.entry_id:
                continue
            if reauth_flow := next(
                entry.async_get_active_flows(self.hass, {SOURCE_REAUTH}), None
            ):
                await self.hass.config_entries.async_reload(entry.entry_id)
                if entry.state == ConfigEntryState.LOADED:
                    self.hass.config_entries.flow.async_abort(reauth_flow["flow_id"])

    @callback
    def _async_create_entry_from_device(self, device: SmartDevice) -> FlowResult:
        """Create a config entry from a smart device."""
        self._abort_if_unique_id_configured(updates={CONF_HOST: device.host})
        return self.async_create_entry(
            title=f"{device.alias} {device.model}",
            data={
                CONF_HOST: device.host,
                CONF_ALIAS: device.alias,
                CONF_MODEL: device.model,
                CONF_DEVICE_TYPE: device.device_type.value,
                CONF_CONNECTION_PARAMS: device.connection_parameters.to_dict()
                if device.connection_parameters
                else None,
            },
        )

    async def _async_try_discover_connect(
        self,
        host: str,
        credentials: Optional[Credentials],
        raise_on_progress: bool = True,
    ) -> tuple[SmartDevice, Optional[SmartDeviceException]]:
        """Try to connect."""
        self._async_abort_entries_match({CONF_HOST: host})
        device: SmartDevice = await Discover.discover_single(
            host, credentials=credentials
        )
        await self.async_set_unique_id(
            dr.format_mac(device.mac), raise_on_progress=raise_on_progress
        )
        try:
            await device.update()
        except SmartDeviceException as ex:
            return device, ex
        return device, None

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Handle reauth upon an API authentication error."""
        self.reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Dialog that informs the user that reauth is required."""
        errors = {}
        assert self.reauth_entry is not None

        if user_input:
            host = self.reauth_entry.data[CONF_HOST]
            credentials = Credentials(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            try:
                device: SmartDevice = await Discover.discover_single(
                    host, credentials=credentials
                )
                await device.update()
            except AuthenticationException:
                errors["base"] = "invalid_auth"
            except SmartDeviceException:
                errors["base"] = "unknown"
            else:
                await set_stored_credentials(
                    self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
                await self.hass.config_entries.async_reload(self.reauth_entry.entry_id)
                async_call_later(
                    self.hass, 0.5, self._async_reload_requires_auth_entries
                )
                return self.async_abort(reason="reauth_successful")

        # Old config entries will not have these values.
        alias = self.reauth_entry.data.get(CONF_ALIAS) or "[alias]"
        model = self.reauth_entry.data.get(CONF_MODEL) or "[model]"

        placeholders = {
            "name": alias,
            "model": model,
            "host": self.reauth_entry.data.get(CONF_HOST),
        }
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_AUTH_DATA_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )
