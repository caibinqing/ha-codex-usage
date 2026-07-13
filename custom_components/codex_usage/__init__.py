"""Codex Usage integration for Home Assistant."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import timedelta
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
    USAGE_CONFIRM_CONSENSUS_TOLERANCE,
    USAGE_CONFIRM_DELAY_SECONDS,
    USAGE_DROP_CONFIRM_THRESHOLD,
    USAGE_RISE_CONFIRM_THRESHOLD,
    USAGE_API_URL,
)
from .usage import (
    async_resolve_usage_confirmation,
    mark_usage_unknown,
    parse_usage,
    stabilize_reset_times,
    suspicious_usage_keys,
    update_trusted_usage,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

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


async def _async_update_listener(
    hass: HomeAssistant, entry: CodexUsageConfigEntry
) -> None:
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
        self._last_trusted_usage: dict[str, float] = {}
        self._reported_usage_issues: set[str] = set()
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

        candidate = await self._async_fetch_usage(headers, proxy)
        suspicious_keys = suspicious_usage_keys(
            candidate,
            self._last_trusted_usage,
            drop_threshold=USAGE_DROP_CONFIRM_THRESHOLD,
            rise_threshold=USAGE_RISE_CONFIRM_THRESHOLD,
        )

        confirmation_error: UpdateFailed | None = None
        try:
            parsed, confirmation = await async_resolve_usage_confirmation(
                candidate,
                lambda: self._async_fetch_usage(headers, proxy),
                asyncio.sleep,
                self._last_trusted_usage,
                suspicious_keys,
                delay_seconds=USAGE_CONFIRM_DELAY_SECONDS,
                consensus_tolerance=USAGE_CONFIRM_CONSENSUS_TOLERANCE,
            )
        except UpdateFailed as err:
            confirmation_error = err
            parsed = mark_usage_unknown(candidate, suspicious_keys)
        else:
            if confirmation is not None:
                for key in sorted(suspicious_keys):
                    if parsed.get(key) is None:
                        if key not in self._reported_usage_issues:
                            _LOGGER.warning(
                                "Ignoring ambiguous %s samples (trusted=%s, "
                                "first=%s, confirmation=%s)",
                                key,
                                self._last_trusted_usage.get(key),
                                candidate.get(key),
                                confirmation.get(key),
                            )
                    elif parsed.get(key) != candidate.get(key):
                        _LOGGER.debug(
                            "Confirmed %s as %s after suspicious sample %s",
                            key,
                            parsed[key],
                            candidate.get(key),
                        )

        if confirmation_error is not None:
            for key in sorted(suspicious_keys - self._reported_usage_issues):
                _LOGGER.warning(
                    "Could not confirm suspicious %s value: %s",
                    key,
                    confirmation_error,
                )

        self._reported_usage_issues.update(
            key for key in suspicious_keys if parsed.get(key) is None
        )
        for key in tuple(self._reported_usage_issues):
            if parsed.get(key) is not None:
                _LOGGER.info("Usage data for %s recovered", key)
                self._reported_usage_issues.remove(key)

        stabilize_reset_times(parsed, self.data, RESET_TIME_JITTER_SECONDS)
        update_trusted_usage(self._last_trusted_usage, parsed)
        return parsed

    async def _async_fetch_usage(
        self, headers: dict[str, str], proxy: str | None
    ) -> dict[str, Any]:
        """Fetch and parse one usage API sample."""
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            resp = await session.get(
                USAGE_API_URL,
                headers=headers,
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=15),
            )
            if resp.status in (401, 403):
                raise ConfigEntryAuthFailed(
                    "Authentication failed - token may be invalid"
                )
            resp.raise_for_status()
            raw = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise UpdateFailed(f"Error fetching usage data: {err}") from err

        if not isinstance(raw, dict):
            raise UpdateFailed("Usage API returned a non-object response")
        return parse_usage(raw)

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
