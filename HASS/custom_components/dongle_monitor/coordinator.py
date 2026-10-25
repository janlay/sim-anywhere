"""Data coordinator for the SIM Anywhere integration."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DEFAULT_TIMEOUT, RECONNECT_SECONDS, STALL_SECONDS, STATUS_PATH

_LOGGER = logging.getLogger(__name__)


def base_url(host: str) -> str:
    """Return the node URL for a host or URL."""
    host = host.strip().rstrip("/")
    return host if host.startswith(("http://", "https://")) else f"http://{host}"


async def async_fetch_sample(hass: HomeAssistant, host: str) -> dict[str, Any]:
    """Read the first record of the status stream (used by the config flow)."""
    session = async_get_clientsession(hass)
    async with session.get(
        base_url(host) + STATUS_PATH + "?seconds=1",
        timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
    ) as resp:
        resp.raise_for_status()
        async for raw in resp.content:
            record = json.loads(raw)
            if "present" in record:
                return record
    raise ValueError("Unexpected dongle status payload")


class DongleDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Consume the status stream. The node sends one record per second.

    The byte counters are merged into the latest data on every record. The
    entities update once a minute, when the node sends the AT-based fields
    (sample_epoch). The byte counters are totals, so a minute resolution is
    enough for Home Assistant.
    """

    def __init__(self, hass: HomeAssistant, host: str) -> None:
        super().__init__(hass, logger=_LOGGER, name="SIM Anywhere", update_interval=None)
        self.host = host
        self.base_url = base_url(host)
        self.first_sample = asyncio.Event()
        self._record: dict[str, Any] = {}
        self._task: asyncio.Task[None] | None = None

    def async_start(self) -> None:
        """Start the background stream task."""
        self._task = self.hass.async_create_background_task(self._run(), "dongle_monitor_stream")

    async def async_stop(self) -> None:
        """Stop the background stream task."""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self._consume()
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                self.async_set_update_error(err)
                _LOGGER.debug("Status stream failed: %s", err)
                await asyncio.sleep(RECONNECT_SECONDS)
            # The node ends the stream after its maximum lifetime. Reconnect at once.

    async def _consume(self) -> None:
        session = async_get_clientsession(self.hass)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=DEFAULT_TIMEOUT, sock_read=STALL_SECONDS)
        async with session.get(self.base_url + STATUS_PATH, timeout=timeout) as resp:
            resp.raise_for_status()
            async for raw in resp.content:
                if raw.strip():
                    self._merge(json.loads(raw))

    def _merge(self, record: dict[str, Any]) -> None:
        self._record.update(record)
        if "sample_epoch" in record:
            self.first_sample.set()
            self.async_set_updated_data(dict(self._record))
