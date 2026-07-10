"""Config flow for Codex Usage integration."""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from . import jwt_expires_at
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_ID,
    CONF_EXPIRES_AT,
    CONF_PROXY_URL,
    CONF_REFRESH_TOKEN,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    USAGE_API_URL,
)

_LOGGER = logging.getLogger(__name__)

CONF_AUTH_JSON = "auth_json"

AUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AUTH_JSON): TextSelector(TextSelectorConfig(multiline=True)),
    }
)

USER_SCHEMA = AUTH_SCHEMA.extend(
    {
        vol.Optional(CONF_PROXY_URL, default=""): str,
    }
)


def _parse_auth_json(text: str) -> dict[str, Any] | None:
    """Extract credentials from pasted ~/.codex/auth.json content."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    tokens = parsed.get("tokens") or {}
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not access_token or not refresh_token:
        return None
    return {
        CONF_ACCESS_TOKEN: access_token,
        CONF_REFRESH_TOKEN: refresh_token,
        CONF_ACCOUNT_ID: tokens.get("account_id"),
        CONF_EXPIRES_AT: jwt_expires_at(access_token) or 0,
    }


class CodexUsageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Codex Usage."""

    VERSION = 1

    async def _validate_credentials(self, creds: dict[str, Any], proxy: str | None) -> bool:
        """Check the credentials against the usage API."""
        headers = {
            "Authorization": f"Bearer {creds[CONF_ACCESS_TOKEN]}",
        }
        if creds.get(CONF_ACCOUNT_ID):
            headers["ChatGPT-Account-Id"] = creds[CONF_ACCOUNT_ID]
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            resp = await session.get(
                USAGE_API_URL,
                headers=headers,
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=15),
            )
        except aiohttp.ClientError:
            _LOGGER.exception("Usage API validation request failed")
            return False
        return resp.ok

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step: paste auth.json contents."""
        errors: dict[str, str] = {}

        if user_input is not None:
            proxy = (user_input.get(CONF_PROXY_URL) or "").strip()
            creds = _parse_auth_json(user_input[CONF_AUTH_JSON])
            if creds is None:
                errors[CONF_AUTH_JSON] = "invalid_auth_json"
            elif proxy and not proxy.startswith("http://"):
                errors[CONF_PROXY_URL] = "invalid_proxy"
            elif not await self._validate_credentials(creds, proxy or None):
                errors[CONF_AUTH_JSON] = "cannot_connect"
            else:
                await self.async_set_unique_id(creds.get(CONF_ACCOUNT_ID) or DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Codex Usage",
                    data=creds,
                    options={
                        CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
                        CONF_PROXY_URL: proxy,
                    },
                )

        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauth when the token is invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth: paste fresh auth.json contents."""
        errors: dict[str, str] = {}

        if user_input is not None:
            entry = self._get_reauth_entry()
            proxy = (entry.options.get(CONF_PROXY_URL) or "").strip() or None
            creds = _parse_auth_json(user_input[CONF_AUTH_JSON])
            if creds is None:
                errors[CONF_AUTH_JSON] = "invalid_auth_json"
            elif not await self._validate_credentials(creds, proxy):
                errors[CONF_AUTH_JSON] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=creds)

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=AUTH_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow."""
        return CodexUsageOptionsFlow()


class CodexUsageOptionsFlow(OptionsFlow):
    """Handle options for Codex Usage."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            proxy = (user_input.get(CONF_PROXY_URL) or "").strip()
            if proxy and not proxy.startswith("http://"):
                errors[CONF_PROXY_URL] = "invalid_proxy"
            else:
                user_input[CONF_PROXY_URL] = proxy
                return self.async_create_entry(data=user_input)

        current_interval = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )
        current_proxy = self.config_entry.options.get(CONF_PROXY_URL, "")
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_UPDATE_INTERVAL, default=current_interval): vol.All(
                        int, vol.Range(min=60, max=3600)
                    ),
                    vol.Optional(CONF_PROXY_URL, default=current_proxy): str,
                }
            ),
            errors=errors,
        )
