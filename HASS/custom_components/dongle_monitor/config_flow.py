"""Config flow for the SIM Anywhere integration."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries

from .const import CONF_HOST, DEFAULT_HOST, DOMAIN
from .coordinator import async_fetch_sample


class DongleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a SIM Anywhere config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the user setup step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                await async_fetch_sample(self.hass, host)
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(host.casefold())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"SIM Anywhere ({host})", data={CONF_HOST: host}
                )

        schema = vol.Schema({vol.Required(CONF_HOST, default=DEFAULT_HOST): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
