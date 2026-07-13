"""Binary sensor platform for Codex Usage integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CodexUsageConfigEntry, CodexUsageCoordinator
from .const import BINARY_SENSOR_DEFINITIONS, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CodexUsageConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Codex Usage binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        CodexUsageBinarySensor(coordinator, entry, key, name, icon)
        for key, name, icon in BINARY_SENSOR_DEFINITIONS
    )


class CodexUsageBinarySensor(
    CoordinatorEntity[CodexUsageCoordinator], BinarySensorEntity
):
    """A binary sensor for a Codex usage flag."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: CodexUsageCoordinator,
        entry: CodexUsageConfigEntry,
        key: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Codex Usage",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Return True if the flag is present in coordinator data."""
        if not super().available or self.coordinator.data is None:
            return False
        return self._key in self.coordinator.data

    @property
    def is_on(self) -> bool | None:
        """Return the flag value, or None when the API sent something odd."""
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self._key)
        return value if isinstance(value, bool) else None
