"""Codex Usage integration for Home Assistant."""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_ID,
    CONF_EXPIRES_AT,
    CONF_PROXY_URL,
    CONF_REFRESH_TOKEN,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    OAUTH_CLIENT_ID,
    OAUTH_TOKEN_URL,
    RESET_TIME_JITTER_SECONDS,
    USAGE_API_URL,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

type CodexUsageConfigEntry = ConfigEntry[CodexUsageCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: CodexUsageConfigEntry) -> bool:
    """Set up Codex Usage from a config entry."""
    coordinator = CodexUsageCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CodexUsageConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: CodexUsageConfigEntry) -> None:
    """Handle options update."""
    interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
    entry.runtime_data.update_interval = timedelta(seconds=interval)


def jwt_expires_at(token: str) -> float | None:
    """Extract the exp claim from a JWT access token, or None."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp is not None else None
    except (IndexError, ValueError, TypeError):
        return None


class CodexUsageCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch Codex usage data."""

    config_entry: CodexUsageConfigEntry

    def __init__(self, hass: HomeAssistant, entry: CodexUsageConfigEntry) -> None:
        """Initialize the coordinator."""
        interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
            config_entry=entry,
            always_update=False,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch usage data from the API."""
        await self._ensure_valid_token()

        headers = {
            "Authorization": f"Bearer {self.config_entry.data[CONF_ACCESS_TOKEN]}",
        }
        account_id = self.config_entry.data.get(CONF_ACCOUNT_ID)
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        proxy = (self.config_entry.options.get(CONF_PROXY_URL) or "").strip() or None

        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            resp = await session.get(
                USAGE_API_URL,
                headers=headers,
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=15),
            )
            if resp.status in (401, 403):
                raise ConfigEntryAuthFailed("Authentication failed - token may be invalid")
            resp.raise_for_status()
            raw = await resp.json()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error fetching usage data: {err}") from err

        parsed = parse_usage(raw)
        _stabilize_reset_times(parsed, self.data)
        return parsed

    async def _ensure_valid_token(self) -> None:
        """Refresh the access token if it is about to expire."""
        expires_at = self.config_entry.data.get(CONF_EXPIRES_AT, 0)
        if time.time() < expires_at - 60:
            return

        refresh_token = self.config_entry.data.get(CONF_REFRESH_TOKEN)
        if not refresh_token:
            raise ConfigEntryAuthFailed("No refresh token available")

        proxy = (self.config_entry.options.get(CONF_PROXY_URL) or "").strip() or None
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            resp = await session.post(
                OAUTH_TOKEN_URL,
                json={
                    "client_id": OAUTH_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": "openid profile email",
                },
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=15),
            )
            if not resp.ok:
                raise ConfigEntryAuthFailed(f"Token refresh failed ({resp.status})")
            token_data = await resp.json()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Token refresh request failed: {err}") from err

        access_token = token_data.get("access_token")
        if not access_token:
            raise ConfigEntryAuthFailed("Token refresh response missing access_token")

        expires_at = jwt_expires_at(access_token)
        if expires_at is None:
            expires_at = time.time() + token_data.get("expires_in", 3600)
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_ACCESS_TOKEN: access_token,
                CONF_REFRESH_TOKEN: token_data.get("refresh_token", refresh_token),
                CONF_EXPIRES_AT: expires_at,
            },
        )


def _window(limits: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    """Return the first present rate-limit window under any of the given keys."""
    for key in keys:
        window = limits.get(key)
        if isinstance(window, dict):
            return window
    return None


def _reset_time_iso(window: dict[str, Any]) -> str | None:
    """Extract the window's reset time as an ISO timestamp string.

    The undocumented API has been observed with both absolute ("resets_at",
    epoch seconds or ISO string) and relative ("resets_in_seconds" /
    "reset_after_seconds") fields depending on version.
    """
    for key in ("resets_at", "reset_at"):
        val = window.get(key)
        if isinstance(val, (int, float)):
            return datetime.fromtimestamp(val, UTC).isoformat()
        if isinstance(val, str) and val:
            return val
    for key in ("resets_in_seconds", "reset_after_seconds"):
        val = window.get(key)
        if isinstance(val, (int, float)):
            return (datetime.now(UTC) + timedelta(seconds=val)).isoformat()
    return None


def parse_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse raw API response into a flat sensor data dict."""
    data: dict[str, Any] = {}

    limits = raw.get("rate_limits") or raw.get("rate_limit") or {}
    primary = _window(limits, "primary", "primary_window")
    if primary:
        data["session_usage_percent"] = primary.get("used_percent")
        data["session_reset_time"] = _reset_time_iso(primary)

    secondary = _window(limits, "secondary", "secondary_window")
    if secondary:
        data["week_usage_percent"] = secondary.get("used_percent")
        data["week_reset_time"] = _reset_time_iso(secondary)

    plan = raw.get("plan_type")
    if plan:
        data["plan_type"] = str(plan)

    return data


def _stabilize_reset_times(new: dict[str, Any], old: dict[str, Any] | None) -> None:
    """Suppress sub-threshold jitter in reset timestamps, in place."""
    if not old:
        return
    for key, new_val in new.items():
        if not key.endswith("_reset_time"):
            continue
        old_val = old.get(key)
        if not new_val or not old_val:
            continue
        try:
            drift = (
                datetime.fromisoformat(new_val) - datetime.fromisoformat(old_val)
            ).total_seconds()
        except (ValueError, TypeError):
            continue
        if abs(drift) < RESET_TIME_JITTER_SECONDS:
            new[key] = old_val
