"""Sensor platform for Codex Usage integration."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CodexUsageConfigEntry, CodexUsageCoordinator
from .const import ADDITIONAL_LIMIT_SENSOR_FIELDS, DOMAIN, SENSOR_DEFINITIONS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CodexUsageConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Codex Usage sensors."""
    coordinator = entry.runtime_data
    entities: list[CodexUsageSensor] = [
        CodexUsageSensor(coordinator, entry, key, name, unit, icon, device_class)
        for key, name, unit, icon, device_class in SENSOR_DEFINITIONS
    ]

    # Per-model limits are discovered from the first refresh; a limit that
    # first appears later gets its sensors on the next reload or restart.
    additional_limits = (coordinator.data or {}).get("additional_limits")
    if isinstance(additional_limits, dict):
        for feature, limit in additional_limits.items():
            limit_name = limit.get("name") or feature
            entities.extend(
                CodexUsageAdditionalLimitSensor(
                    coordinator,
                    entry,
                    feature,
                    field,
                    f"{limit_name} {name_suffix}",
                    unit,
                    icon,
                    device_class,
                )
                for field, name_suffix, unit, icon, device_class in (
                    ADDITIONAL_LIMIT_SENSOR_FIELDS
                )
            )

    async_add_entities(entities)


class CodexUsageSensor(CoordinatorEntity[CodexUsageCoordinator], SensorEntity):
    """A sensor for a Codex usage metric."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CodexUsageCoordinator,
        entry: CodexUsageConfigEntry,
        key: str,
        name: str,
        unit: str | None,
        icon: str,
        device_class: str | None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._key = key
        self._is_timestamp = device_class == "timestamp"
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        if self._is_timestamp:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        elif unit is not None:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Codex Usage",
            entry_type=DeviceEntryType.SERVICE,
        )

    def _lookup_value(self) -> tuple[bool, Any]:
        """Return whether the value is present in coordinator data, and it."""
        data = self.coordinator.data
        if data is None or self._key not in data:
            return False, None
        return True, data[self._key]

    @property
    def available(self) -> bool:
        """Return True if the sensor value is present in coordinator data."""
        if not super().available:
            return False
        present, _ = self._lookup_value()
        return present

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        _, value = self._lookup_value()
        if value is not None and self._is_timestamp:
            try:
                return datetime.fromisoformat(value)
            except (ValueError, TypeError):
                _LOGGER.warning("Invalid timestamp value for %s: %s", self._key, value)
                return None
        return value


class CodexUsageAdditionalLimitSensor(CodexUsageSensor):
    """A sensor for one field of a per-model additional rate limit."""

    def __init__(
        self,
        coordinator: CodexUsageCoordinator,
        entry: CodexUsageConfigEntry,
        feature: str,
        field: str,
        name: str,
        unit: str | None,
        icon: str,
        device_class: str | None,
    ) -> None:
        """Initialize the sensor from the limit's stable feature slug."""
        super().__init__(
            coordinator,
            entry,
            f"additional_{feature}_{field}",
            name,
            unit,
            icon,
            device_class,
        )
        self._feature = feature
        self._field = field

    def _lookup_value(self) -> tuple[bool, Any]:
        """Look the value up under the nested additional_limits mapping."""
        data = self.coordinator.data or {}
        limits = data.get("additional_limits")
        if not isinstance(limits, dict):
            return False, None
        window = limits.get(self._feature)
        if not isinstance(window, dict) or self._field not in window:
            return False, None
        return True, window[self._field]
