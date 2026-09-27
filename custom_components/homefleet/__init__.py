"""Periodically send current Home Assistant health to BlackLabs Watchdog."""

import asyncio
from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started

from .api import BackendResponseError, BackendUnavailableError, InvalidKeyError, request
from .collector import collect
from .const import CONF_INTEGRATION_KEY, CONF_INTERVAL, CONF_URL, DEFAULT_INTERVAL, DISPLAY_NAME, DOMAIN, MAX_INTERVAL, REPORT_TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)


class Reporter:
    """Own the timer and active request for one config entry."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self._lock = asyncio.Lock()
        self._tasks = set()
        self._stopped = False
        self._cancel_start = None
        self._cancel_timer = None

    def start(self):
        """Schedule the first and subsequent snapshots."""
        self._cancel_start = async_at_started(self.hass, self._started)

    @callback
    def _started(self, _):
        """Start reporting only once Home Assistant has finished startup."""
        if self._stopped:
            return
        interval = self.entry.options.get(CONF_INTERVAL, self.entry.data.get(CONF_INTERVAL, DEFAULT_INTERVAL))
        if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= MAX_INTERVAL:
            # Older entries can contain values accepted before interval validation.
            _LOGGER.warning("BlackLabs Watchdog má neplatný interval; používa predvolených 5 minút")
            interval = DEFAULT_INTERVAL
        self._cancel_timer = async_track_time_interval(self.hass, self._schedule, timedelta(minutes=interval))
        self._schedule()

    @callback
    def _schedule(self, _=None):
        if self._stopped or self._lock.locked():
            return
        task = self.hass.async_create_task(self.send())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(self):
        """Discard a failed report and gather new data on the next tick."""
        if self._stopped or self._lock.locked():
            return
        async with self._lock:
            try:
                async with asyncio.timeout(REPORT_TIMEOUT_SECONDS):
                    payload = await collect(self.hass, self.entry)
                    if payload.get("isComplete") is False:
                        _LOGGER.warning("BlackLabs Watchdog odosiela neúplný report: %s",
                                        payload.get("errorMessage") or "dôvod nie je dostupný")
                    await request(self.hass, self.entry.data[CONF_URL], self.entry.data[CONF_INTEGRATION_KEY],
                                  "POST", "/api/ha-integration/lifechecks", payload)
            except InvalidKeyError:
                _LOGGER.error("BlackLabs Watchdog: server odmietol integračný kľúč")
            except BackendResponseError as err:
                _LOGGER.warning("BlackLabs Watchdog: server odmietol report: HTTP %s (%s)", err.status, err.reason)
            except BackendUnavailableError as err:
                _LOGGER.warning("BlackLabs Watchdog sa nepodarilo kontaktovať (%s)", err.reason)
            except TimeoutError:
                _LOGGER.warning("BlackLabs Watchdog prekročil časový limit pokusu; ďalší interval vytvorí nový report")
            except Exception as err:
                # Exception text/tracebacks may contain source values or credentials.
                _LOGGER.error("BlackLabs Watchdog nedokázal zostaviť alebo odoslať report (%s)", type(err).__name__)

    async def stop(self):
        """Stop scheduling before cancelling and awaiting active tasks."""
        self._stopped = True
        if self._cancel_start is not None:
            self._cancel_start()
            self._cancel_start = None
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    if entry.title != DISPLAY_NAME:
        hass.config_entries.async_update_entry(entry, title=DISPLAY_NAME)
    reporter = Reporter(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = reporter
    entry.async_on_unload(entry.add_update_listener(_reload_entry))
    reporter.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    reporter = hass.data[DOMAIN].pop(entry.entry_id)
    await reporter.stop()
    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)
    return True


async def _reload_entry(hass: HomeAssistant, entry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
