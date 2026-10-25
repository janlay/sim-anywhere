"""Sensors for the dongle and its SIM card."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfInformation,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, PAGE_PATH
from .coordinator import DongleDataUpdateCoordinator

DONGLE_ATTRS = (
    "model", "imei", "usb_port", "at_port", "usb0_carrier", "usb0_ip",
    "link_uptime", "disconnects", "netifd_up", "host_ip", "ts",
)
SIM_ATTRS = (
    "iccid", "imsi", "oper", "reg", "band", "rrc",
    "cellid", "pci", "earfcn", "apn", "wan_ip", "wan_ip6", "dns",
)
DONGLE_STATES = ["online", "absent", "link_down", "no_modem_data"]
SIM_STATES = ["ready", "no_sim", "locked", "unknown"]


def _signal(key: str, unit: str) -> SensorEntityDescription:
    return SensorEntityDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=unit,
        state_class=SensorStateClass.MEASUREMENT,
    )


def _traffic(key: str) -> SensorEntityDescription:
    # usb0 byte counters restart from zero when the node reboots
    return SensorEntityDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.TOTAL_INCREASING,
    )


DONGLE_VALUES = (
    SensorEntityDescription(
        key="temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _traffic("rx"),
    _traffic("tx"),
)
SIM_VALUES = (
    _signal("rsrp", SIGNAL_STRENGTH_DECIBELS_MILLIWATT),
    _signal("rsrq", SIGNAL_STRENGTH_DECIBELS),
    _signal("sinr", SIGNAL_STRENGTH_DECIBELS),
    _signal("rssi", SIGNAL_STRENGTH_DECIBELS_MILLIWATT),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the dongle and SIM sensors."""
    coordinator: DongleDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    dongle = dongle_device(coordinator, entry)
    sim = sim_device(coordinator, entry)
    async_add_entities(
        [
            DongleSensor(coordinator, entry, dongle),
            SimSensor(coordinator, entry, sim),
            *(DongleValueSensor(coordinator, entry, dongle, d) for d in DONGLE_VALUES),
            *(SimValueSensor(coordinator, entry, sim, d) for d in SIM_VALUES),
        ]
    )


def dongle_device(coordinator: DongleDataUpdateCoordinator, entry: ConfigEntry) -> DeviceInfo:
    """Build the dongle device information."""
    # "Baiwang QDC507 QDC507GLEFM21" = vendor, model, firmware revision
    parts = (coordinator.data.get("model") or "").split()
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_dongle")},
        translation_key="dongle",
        manufacturer=parts[0] if parts else None,
        model=parts[1] if len(parts) > 1 else None,
        sw_version=parts[2] if len(parts) > 2 else None,
        serial_number=coordinator.data.get("imei"),
        configuration_url=coordinator.base_url + PAGE_PATH,
    )


def sim_device(coordinator: DongleDataUpdateCoordinator, entry: ConfigEntry) -> DeviceInfo:
    """Build the SIM card device information."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_sim")},
        translation_key="sim",
        model="SIM",
        serial_number=coordinator.data.get("iccid"),
    )


class DongleEntity(CoordinatorEntity[DongleDataUpdateCoordinator], SensorEntity):
    """Base sensor of one device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DongleDataUpdateCoordinator,
        entry: ConfigEntry,
        device: DeviceInfo,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = device


class DongleStatusSensor(DongleEntity):
    """The main sensor of a device; the remaining sample fields are its attributes."""

    _attr_name = None
    _attr_device_class = SensorDeviceClass.ENUM
    _attrs: tuple[str, ...] = ()

    def __init__(
        self,
        coordinator: DongleDataUpdateCoordinator,
        entry: ConfigEntry,
        device: DeviceInfo,
        key: str,
    ) -> None:
        super().__init__(coordinator, entry, device, key)
        self._attr_translation_key = key

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the sample fields that belong to this device."""
        return {key: self.coordinator.data.get(key) for key in self._attrs}


class DongleSensor(DongleStatusSensor):
    """Link state of the dongle module."""

    _attr_icon = "mdi:usb-flash-drive"
    _attr_options = DONGLE_STATES
    _attrs = DONGLE_ATTRS

    def __init__(
        self, coordinator: DongleDataUpdateCoordinator, entry: ConfigEntry, device: DeviceInfo
    ) -> None:
        super().__init__(coordinator, entry, device, "dongle")

    @property
    def native_value(self) -> str:
        """Return the link verdict, in the same order as the monitor page."""
        data = self.coordinator.data
        if data.get("present") != 1:
            return "absent"
        if data.get("netifd_up") != 1:
            return "link_down"
        if not data.get("at_port"):
            return "no_modem_data"
        return "online"


class SimSensor(DongleStatusSensor):
    """State of the SIM card in the dongle."""

    _attr_icon = "mdi:sim"
    _attr_options = SIM_STATES
    _attrs = SIM_ATTRS

    def __init__(
        self, coordinator: DongleDataUpdateCoordinator, entry: ConfigEntry, device: DeviceInfo
    ) -> None:
        super().__init__(coordinator, entry, device, "sim")

    @property
    def available(self) -> bool:
        """The SIM is only readable through the module's AT port."""
        return super().available and bool(self.coordinator.data.get("at_port"))

    @property
    def native_value(self) -> str | None:
        """Return the SIM state reported by AT+CPIN."""
        return self.coordinator.data.get("sim")


class DongleValueSensor(DongleEntity):
    """One numeric field of the sample."""

    def __init__(
        self,
        coordinator: DongleDataUpdateCoordinator,
        entry: ConfigEntry,
        device: DeviceInfo,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, entry, device, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | None:
        """Return the field value; null fields are unknown."""
        return self.coordinator.data.get(self.entity_description.key)


class SimValueSensor(DongleValueSensor):
    """Signal value of the serving cell, read through the AT port."""

    @property
    def available(self) -> bool:
        """The SIM is only readable through the module's AT port."""
        return super().available and bool(self.coordinator.data.get("at_port"))
