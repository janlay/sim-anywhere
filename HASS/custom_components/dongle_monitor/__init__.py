"""SIM Anywhere integration."""

from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_HOST, DOMAIN, FIRST_SAMPLE_SECONDS, PLATFORMS
from .coordinator import DongleDataUpdateCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a configured dongle node."""
    coordinator = DongleDataUpdateCoordinator(hass, entry.data[CONF_HOST])
    coordinator.async_start()
    entry.async_on_unload(coordinator.async_stop)
    try:
        await asyncio.wait_for(coordinator.first_sample.wait(), FIRST_SAMPLE_SECONDS)
    except TimeoutError as err:
        raise ConfigEntryNotReady("No status record from the dongle node") from err
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a configured dongle node."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
