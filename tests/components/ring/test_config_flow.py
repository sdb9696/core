"""Test the Ring config flow."""

from unittest.mock import AsyncMock, Mock

import pytest
import ring_doorbell

from homeassistant import config_entries
from homeassistant.components import dhcp
from homeassistant.components.ring import CONF_LIVE_STREAM, DOMAIN
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from tests.common import MockConfigEntry


async def test_form(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    mock_ring_client: Mock,
) -> None:
    """Test we get the form."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"username": "hello@home-assistant.io", "password": "test-password"},
    )
    await hass.async_block_till_done()

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["title"] == "hello@home-assistant.io"
    assert result2["data"] == {
        "username": "hello@home-assistant.io",
        "token": {"access_token": "mock-token"},
    }
    assert len(mock_setup_entry.mock_calls) == 1


@pytest.mark.parametrize(
    ("error_type", "errors_msg"),
    [
        (ring_doorbell.AuthenticationError, "invalid_auth"),
        (Exception, "unknown"),
    ],
    ids=["invalid-auth", "unknown-error"],
)
async def test_form_error(
    hass: HomeAssistant, mock_ring_auth: Mock, error_type, errors_msg
) -> None:
    """Test we handle invalid auth."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    mock_ring_auth.async_fetch_token.side_effect = error_type
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"username": "hello@home-assistant.io", "password": "test-password"},
    )

    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"] == {"base": errors_msg}


async def test_form_2fa(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    mock_ring_auth: Mock,
) -> None:
    """Test form flow for 2fa."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    mock_ring_auth.async_fetch_token.side_effect = ring_doorbell.Requires2FAError
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_USERNAME: "foo@bar.com",
            CONF_PASSWORD: "fake-password",
        },
    )
    await hass.async_block_till_done()
    mock_ring_auth.async_fetch_token.assert_called_once_with(
        "foo@bar.com", "fake-password", None
    )

    assert result2["type"] is FlowResultType.FORM
    assert result2["step_id"] == "2fa"
    mock_ring_auth.async_fetch_token.reset_mock(side_effect=True)
    mock_ring_auth.async_fetch_token.return_value = "new-foobar"
    result3 = await hass.config_entries.flow.async_configure(
        result2["flow_id"],
        user_input={"2fa": "123456"},
    )

    mock_ring_auth.async_fetch_token.assert_called_once_with(
        "foo@bar.com", "fake-password", "123456"
    )
    assert result3["type"] is FlowResultType.CREATE_ENTRY
    assert result3["title"] == "foo@bar.com"
    assert result3["data"] == {
        "username": "foo@bar.com",
        "token": "new-foobar",
    }
    assert len(mock_setup_entry.mock_calls) == 1


async def test_reauth(
    hass: HomeAssistant,
    mock_added_config_entry: MockConfigEntry,
    mock_setup_entry: AsyncMock,
    mock_ring_auth: Mock,
) -> None:
    """Test reauth flow."""
    mock_added_config_entry.async_start_reauth(hass)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    [result] = flows
    assert result["step_id"] == "reauth_confirm"

    mock_ring_auth.async_fetch_token.side_effect = ring_doorbell.Requires2FAError
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PASSWORD: "other_fake_password",
        },
    )

    mock_ring_auth.async_fetch_token.assert_called_once_with(
        "foo@bar.com", "other_fake_password", None
    )
    assert result2["type"] is FlowResultType.FORM
    assert result2["step_id"] == "2fa"
    mock_ring_auth.async_fetch_token.reset_mock(side_effect=True)
    mock_ring_auth.async_fetch_token.return_value = "new-foobar"
    result3 = await hass.config_entries.flow.async_configure(
        result2["flow_id"],
        user_input={"2fa": "123456"},
    )

    mock_ring_auth.async_fetch_token.assert_called_once_with(
        "foo@bar.com", "other_fake_password", "123456"
    )
    assert result3["type"] is FlowResultType.ABORT
    assert result3["reason"] == "reauth_successful"
    assert mock_added_config_entry.data == {
        "username": "foo@bar.com",
        "token": "new-foobar",
    }
    assert len(mock_setup_entry.mock_calls) == 1


@pytest.mark.parametrize(
    ("error_type", "errors_msg"),
    [
        (ring_doorbell.AuthenticationError, "invalid_auth"),
        (Exception, "unknown"),
    ],
    ids=["invalid-auth", "unknown-error"],
)
async def test_reauth_error(
    hass: HomeAssistant,
    mock_added_config_entry: MockConfigEntry,
    mock_setup_entry: AsyncMock,
    mock_ring_auth: Mock,
    error_type,
    errors_msg,
) -> None:
    """Test reauth flow."""
    mock_added_config_entry.async_start_reauth(hass)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    [result] = flows
    assert result["step_id"] == "reauth_confirm"

    mock_ring_auth.async_fetch_token.side_effect = error_type
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PASSWORD: "error_fake_password",
        },
    )
    await hass.async_block_till_done()

    mock_ring_auth.async_fetch_token.assert_called_once_with(
        "foo@bar.com", "error_fake_password", None
    )
    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"] == {"base": errors_msg}

    # Now test reauth can go on to succeed
    mock_ring_auth.async_fetch_token.reset_mock(side_effect=True)
    mock_ring_auth.async_fetch_token.return_value = "new-foobar"
    result3 = await hass.config_entries.flow.async_configure(
        result2["flow_id"],
        user_input={
            CONF_PASSWORD: "other_fake_password",
        },
    )

    mock_ring_auth.async_fetch_token.assert_called_once_with(
        "foo@bar.com", "other_fake_password", None
    )
    assert result3["type"] is FlowResultType.ABORT
    assert result3["reason"] == "reauth_successful"
    assert mock_added_config_entry.data == {
        "username": "foo@bar.com",
        "token": "new-foobar",
    }
    assert len(mock_setup_entry.mock_calls) == 1


async def test_account_configured(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    mock_added_config_entry: Mock,
) -> None:
    """Test that user cannot configure the same account twice."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"username": "foo@bar.com", "password": "test-password"},
    )

    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


async def test_dhcp_discovery(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    mock_ring_client: Mock,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Test discovery by dhcp."""
    mac_address = "1234567890abcd"
    hostname = "Ring-90abcd"
    ip_address = "127.0.0.1"
    username = "hello@home-assistant.io"

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_DHCP},
        data=dhcp.DhcpServiceInfo(
            ip=ip_address, macaddress=mac_address, hostname=hostname
        ),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"username": username, "password": "test-password"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "hello@home-assistant.io"
    assert result["data"] == {
        "username": username,
        "token": {"access_token": "mock-token"},
    }

    config_entry = hass.config_entries.async_entry_for_domain_unique_id(
        DOMAIN, username
    )
    assert config_entry

    # Create a device entry under the config entry just created
    device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, mac_address)},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_DHCP},
        data=dhcp.DhcpServiceInfo(
            ip=ip_address, macaddress=mac_address, hostname=hostname
        ),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(
    hass: HomeAssistant,
    mock_added_config_entry: MockConfigEntry,
) -> None:
    """Test config flow options."""
    assert mock_added_config_entry.options == {}
    assert mock_added_config_entry.runtime_data.live_stream is True

    # Set the option to False
    result = await hass.config_entries.options.async_init(
        mock_added_config_entry.entry_id
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_LIVE_STREAM: False}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert CONF_LIVE_STREAM in mock_added_config_entry.options
    assert mock_added_config_entry.options[CONF_LIVE_STREAM] is False
    assert mock_added_config_entry.runtime_data.live_stream is False

    # Set the option back to True
    result = await hass.config_entries.options.async_init(
        mock_added_config_entry.entry_id
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_LIVE_STREAM: True}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert CONF_LIVE_STREAM in mock_added_config_entry.options
    assert mock_added_config_entry.options[CONF_LIVE_STREAM] is True
    assert mock_added_config_entry.runtime_data.live_stream is True
