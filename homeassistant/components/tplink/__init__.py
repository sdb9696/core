"""Component to embed TP-Link smart home devices."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Optional

from kasa import (
    AuthenticationException,
    Credentials,
    DeviceType,
    SmartDevice,
    SmartDeviceException,
)
from kasa.connectionparams import ConnectionParameters
from kasa.discover import Discover

from homeassistant import config_entries
from homeassistant.components import network
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_AUTHENTICATION,
    CONF_HOST,
    CONF_MAC,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STARTED,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    discovery_flow,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_CONNECTION_PARAMS,
    CONF_DEVICE_TYPE,
    DOMAIN,
    PLATFORMS,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .coordinator import TPLinkDataUpdateCoordinator

# DISCOVERY_INTERVAL = timedelta(minutes=15)
DISCOVERY_INTERVAL = timedelta(seconds=30)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@callback
def async_trigger_discovery(
    hass: HomeAssistant,
    discovered_devices: dict[str, SmartDevice],
) -> None:
    """Trigger config flows for discovered devices."""
    for formatted_mac, device in discovered_devices.items():
        discovery_flow.async_create_flow(
            hass,
            DOMAIN,
            context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
            data={
                CONF_NAME: device.alias,
                CONF_HOST: device.host,
                CONF_MAC: formatted_mac,
            },
        )


async def async_discover_devices(hass: HomeAssistant) -> dict[str, SmartDevice]:
    """Discover TPLink devices on configured network interfaces."""
    credentials = await get_stored_credentials(hass)
    broadcast_addresses = await network.async_get_ipv4_broadcast_addresses(hass)
    tasks = [
        Discover.discover(target=str(address), credentials=credentials)
        for address in broadcast_addresses
    ]
    discovered_devices: dict[str, SmartDevice] = {}
    for device_list in await asyncio.gather(*tasks):
        for device in device_list.values():
            discovered_devices[dr.format_mac(device.mac)] = device
    return discovered_devices


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the TP-Link component."""
    hass.data[DOMAIN] = {}

    if discovered_devices := await async_discover_devices(hass):
        async_trigger_discovery(hass, discovered_devices)

    async def _async_discovery(*_: Any) -> None:
        if discovered := await async_discover_devices(hass):
            async_trigger_discovery(hass, discovered)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _async_discovery)
    async_track_time_interval(
        hass, _async_discovery, DISCOVERY_INTERVAL, cancel_on_shutdown=True
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TPLink from a config entry."""
    host = entry.data[CONF_HOST]
    try:
        credentials = await get_stored_credentials(hass)
        device_type = None
        connection_params = None

        if device_type_str := entry.data.get(CONF_DEVICE_TYPE):
            device_type = DeviceType.from_value(device_type_str)
        if connection_params_dict := entry.data.get(CONF_CONNECTION_PARAMS):
            connection_params = ConnectionParameters.from_dict(connection_params_dict)

        device: SmartDevice = await SmartDevice.connect(
            host,
            timeout=10,
            credentials=credentials,
            device_type=device_type,
            connection_params=connection_params,
            try_discovery_on_error=True,
        )
        # Save the device class name and connection_params if they are not already saved
        # so that we can pass them to connect which avoids an update cycle
        if device_type_str != device.device_type.value:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_DEVICE_TYPE: device.device_type.value}
            )
        if connection_params_dict and connection_params != device.connection_parameters:
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_CONNECTION_PARAMS: device.connection_parameters.to_dict()
                    if device.connection_parameters
                    else None,
                },
            )
    except AuthenticationException as ex:
        raise ConfigEntryAuthFailed from ex
    except SmartDeviceException as ex:
        raise ConfigEntryNotReady from ex

    found_mac = dr.format_mac(device.mac)
    if found_mac != entry.unique_id:
        # If the mac address of the device does not match the unique_id
        # of the config entry, it likely means the DHCP lease has expired
        # and the device has been assigned a new IP address. We need to
        # wait for the next discovery to find the device at its new address
        # and update the config entry so we do not mix up devices.
        raise ConfigEntryNotReady(
            f"Unexpected device found at {host}; expected {entry.unique_id}, found {found_mac}"
        )

    hass.data[DOMAIN][entry.entry_id] = TPLinkDataUpdateCoordinator(hass, device)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass_data: dict[str, Any] = hass.data[DOMAIN]
    device: SmartDevice = hass_data[entry.entry_id].device
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass_data.pop(entry.entry_id)
    await device.protocol.close()

    if len(hass.config_entries.async_entries(DOMAIN)) == 1:
        await _get_store(hass).async_remove()
    return unload_ok


def legacy_device_id(device: SmartDevice) -> str:
    """Convert the device id so it matches what was used in the original version."""
    device_id: str = device.device_id
    # Plugs are prefixed with the mac in python-kasa but not
    # in pyHS100 so we need to strip off the mac
    if "_" not in device_id:
        return device_id
    return device_id.split("_")[1]


def _get_store(hass: HomeAssistant) -> Store[dict[str, dict[str, str]]]:
    return Store[dict[str, dict[str, str]]](hass, STORAGE_VERSION, STORAGE_KEY)


async def get_stored_credentials(hass: HomeAssistant) -> Optional[Credentials]:
    """Retrieve the credentials from the Store."""
    storage_data = await _get_store(hass).async_load()
    if storage_data and (auth := storage_data[CONF_AUTHENTICATION]):
        return Credentials(auth[CONF_USERNAME], auth[CONF_PASSWORD])

    return None


async def set_stored_credentials(
    hass: HomeAssistant, username: str, password: str
) -> None:
    """Save the credentials to the Store."""
    storage_data = {
        CONF_AUTHENTICATION: {
            CONF_USERNAME: username,
            CONF_PASSWORD: password,
        }
    }
    await _get_store(hass).async_save(storage_data)
