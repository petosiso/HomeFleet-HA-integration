"""UI configuration for BlackLabs Watchdog."""

from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .api import BackendUnavailableError, InvalidKeyError, request
from .const import (
    CONF_ENTITIES,
    CONF_INTEGRATION_KEY,
    CONF_INTERVAL,
    CONF_URL,
    DEFAULT_INTERVAL,
    DISPLAY_NAME,
    DOMAIN,
    MAX_INTERVAL,
)


def valid_url(value: str) -> str:
    """Accept only a plain HTTPS backend origin or base path."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as err:
        raise vol.Invalid("invalid_url") from err
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or port == 0 or any(char.isspace() for char in value)):
        raise vol.Invalid("invalid_url")
    return value.rstrip("/")


def valid_key(value: str) -> str:
    """Validate an existing 256-bit hexadecimal installation key."""
    if len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
        raise vol.Invalid("invalid_key")
    return value


def connection_schema(defaults: dict) -> vol.Schema:
    """Build the connection form."""
    return vol.Schema({
        vol.Required(CONF_URL, default=defaults.get(CONF_URL, "")):
            selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.URL)),
        vol.Required(CONF_INTEGRATION_KEY, default=defaults.get(CONF_INTEGRATION_KEY, "")):
            selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
    })


def monitoring_schema(defaults: dict) -> vol.Schema:
    """Build the monitored-entities form."""
    return vol.Schema({
        vol.Optional(CONF_ENTITIES, default=defaults.get(CONF_ENTITIES, [])):
            selector.EntitySelector(selector.EntitySelectorConfig(multiple=True)),
        vol.Required(CONF_INTERVAL, default=defaults.get(CONF_INTERVAL, DEFAULT_INTERVAL)):
            vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_INTERVAL)),
    })


async def validate_connection(hass, data: dict) -> str | None:
    """Return a form error code when the backend rejects the connection."""
    try:
        url = valid_url(data[CONF_URL])
        key = valid_key(data[CONF_INTEGRATION_KEY])
        await request(hass, url, key, "GET", "/api/ha-integration/connection")
    except vol.Invalid as err:
        return str(err)
    except InvalidKeyError:
        return "invalid_key"
    except BackendUnavailableError:
        return "cannot_connect"
    return None


class HomeFleetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create a single HomeFleet entry per HA instance."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            error = await validate_connection(self.hass, user_input)
            if error is None:
                self._connection = {**user_input, CONF_URL: valid_url(user_input[CONF_URL])}
                return await self.async_step_monitoring()
            return self.async_show_form(step_id="user", data_schema=connection_schema(user_input), errors={"base": error})
        return self.async_show_form(step_id="user", data_schema=connection_schema({}))

    async def async_step_monitoring(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        if user_input is not None:
            return self.async_create_entry(title=DISPLAY_NAME, data={**self._connection, **user_input})
        return self.async_show_form(step_id="monitoring", data_schema=monitoring_schema({}))

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            error = await validate_connection(self.hass, user_input)
            if error is None:
                return self.async_update_and_abort(entry, data={**entry.data,
                    CONF_URL: valid_url(user_input[CONF_URL]),
                    CONF_INTEGRATION_KEY: user_input[CONF_INTEGRATION_KEY]})
            return self.async_show_form(step_id="reconfigure", data_schema=connection_schema(user_input), errors={"base": error})
        return self.async_show_form(step_id="reconfigure", data_schema=connection_schema({CONF_URL: entry.data[CONF_URL]}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HomeFleetOptionsFlow()


class HomeFleetOptionsFlow(config_entries.OptionsFlow):
    """Edit entities and reporting interval."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=monitoring_schema(defaults))
