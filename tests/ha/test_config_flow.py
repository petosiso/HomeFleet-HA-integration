"""Regression tests through Home Assistant's actual flow managers."""

from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.homefleet.api import BackendUnavailableError, InvalidKeyError

CONNECTION = {"url": "https://example.test", "integration_key": "a" * 64}


async def begin_monitoring(hass):
    result = await hass.config_entries.flow.async_init("homefleet", context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.homefleet.config_flow.request"):
        return await hass.config_entries.flow.async_configure(result["flow_id"], CONNECTION)


async def test_single_entry_and_in_progress_guard(hass):
    first = await begin_monitoring(hass)
    second = await hass.config_entries.flow.async_init("homefleet", context={"source": SOURCE_USER})
    assert first["step_id"] == "monitoring"
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_in_progress"
    schema = first["data_schema"]
    assert schema({}) == {"entities": [], "interval": 5}
    with pytest.raises(vol.Invalid):
        schema({"interval": 0})
    with patch("custom_components.homefleet.async_setup_entry", return_value=True):
        created = await hass.config_entries.flow.async_configure(first["flow_id"], schema({}))
        duplicate = await hass.config_entries.flow.async_init("homefleet", context={"source": SOURCE_USER})
    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert duplicate["type"] is FlowResultType.ABORT
    assert duplicate["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries("homefleet")) == 1


@pytest.mark.parametrize(("error", "code"), [(InvalidKeyError(), "invalid_key"), (BackendUnavailableError(), "cannot_connect")])
async def test_connection_errors(hass, error, code):
    result = await hass.config_entries.flow.async_init("homefleet", context={"source": SOURCE_USER})
    with patch("custom_components.homefleet.config_flow.request", side_effect=error):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CONNECTION)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": code}


async def test_malformed_url_is_form_error(hass):
    result = await hass.config_entries.flow.async_init("homefleet", context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**CONNECTION, "url": "https://[bad"})
    assert result["errors"] == {"base": "invalid_url"}


async def test_options_preserve_missing_entity_and_reconfigure_connection(hass):
    entry = MockConfigEntry(domain="homefleet", data={**CONNECTION, "entities": ["sensor.gone"], "interval": 5})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    # The real EntitySelector must allow a previously selected entity that disappeared.
    data = result["data_schema"]({"entities": ["sensor.gone"], "interval": 10})
    result = await hass.config_entries.options.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"entities": ["sensor.gone"], "interval": 10}
    result = await hass.config_entries.flow.async_init("homefleet", context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id})
    with patch("custom_components.homefleet.config_flow.request"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {**CONNECTION, "url": "https://new.test"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["url"] == "https://new.test"
    assert entry.options["entities"] == ["sensor.gone"]
