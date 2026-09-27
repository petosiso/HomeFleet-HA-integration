"""Exercise real HA startup, timers, entry reload and unload."""

import asyncio
from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.homefleet.api import BackendUnavailableError


def add_entry(hass):
    entry = MockConfigEntry(domain="homefleet", data={"url": "https://example.test",
        "integration_key": "a" * 64, "entities": [], "interval": 5})
    entry.add_to_hass(hass)
    return entry


async def tick(hass, freezer, minutes):
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_wait_for_start_then_interval_and_unload(hass, freezer):
    hass.state = CoreState.starting
    entry = add_entry(hass)
    with patch("custom_components.homefleet.collect", return_value={}), patch("custom_components.homefleet.request") as send:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        send.assert_not_called()
        hass.state = CoreState.running
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        assert send.await_count == 1
        await tick(hass, freezer, 4)
        assert send.await_count == 1
        await tick(hass, freezer, 1)
        assert send.await_count == 2
        assert await hass.config_entries.async_unload(entry.entry_id)
        await tick(hass, freezer, 5)
        assert send.await_count == 2
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert "homefleet" not in hass.data


async def test_running_ha_sends_immediately_and_recovers_with_fresh_report(hass, freezer):
    hass.state = CoreState.running
    entry = add_entry(hass)
    with patch("custom_components.homefleet.collect", side_effect=[{"snapshot": 1}, {"snapshot": 2}]), \
         patch("custom_components.homefleet.request", side_effect=[BackendUnavailableError(), None]) as send:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert send.await_count == 1
        await tick(hass, freezer, 5)
        assert [call.args[-1] for call in send.await_args_list] == [{"snapshot": 1}, {"snapshot": 2}]
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_cancels_inflight_request(hass):
    hass.state = CoreState.running
    entry = add_entry(hass)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(*_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with patch("custom_components.homefleet.collect", return_value={}), \
         patch("custom_components.homefleet.request", side_effect=blocked):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(started.wait(), timeout=5)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert cancelled.is_set()


async def test_options_reload_replaces_reporter_and_timer(hass, freezer):
    hass.state = CoreState.running
    entry = add_entry(hass)
    with patch("custom_components.homefleet.collect", return_value={}), patch("custom_components.homefleet.request") as send:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        old_reporter = hass.data["homefleet"][entry.entry_id]
        hass.config_entries.async_update_entry(entry, options={"entities": ["sensor.gone"], "interval": 10})
        await hass.async_block_till_done()
        assert old_reporter is not hass.data["homefleet"][entry.entry_id]
        assert send.await_count == 2
        await tick(hass, freezer, 5)
        assert send.await_count == 2
        await tick(hass, freezer, 5)
        assert send.await_count == 3
        assert await hass.config_entries.async_unload(entry.entry_id)
