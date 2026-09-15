"""Config and options flow for Home Energy Manager."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import HemAuthError, HemError, HomeEnergyManagerApi
from .const import (
    CONF_CONFIRM_TIMEOUT,
    CONF_DASHBOARD_PORT,
    CONF_ENABLE_CONTROLS,
    CONF_FORCE_MINUTES,
    CONF_POLL_SNAPSHOT,
    CONF_USE_SSL,
    DEFAULT_CONFIRM_TIMEOUT,
    DEFAULT_DASHBOARD_PORT,
    DEFAULT_FORCE_MINUTES,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_CONFIRM_TIMEOUT,
    MAX_FORCE_MINUTES,
    MAX_SCAN_INTERVAL,
    MIN_CONFIRM_TIMEOUT,
    MIN_FORCE_MINUTES,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(),
        vol.Required(CONF_PORT, default=DEFAULT_PORT): NumberSelector(
            NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_USE_SSL, default=False): BooleanSelector(),
        vol.Optional(CONF_VERIFY_SSL, default=True): BooleanSelector(),
    }
)


async def _async_validate(hass, data: dict[str, Any]) -> None:
    """Raise if HEM cannot be reached or the key is rejected."""
    session = async_get_clientsession(hass, verify_ssl=data.get(CONF_VERIFY_SSL, True))
    api = HomeEnergyManagerApi(
        session,
        host=data[CONF_HOST],
        port=int(data[CONF_PORT]),
        api_key=data[CONF_API_KEY],
        use_ssl=data.get(CONF_USE_SSL, False),
    )
    # A successful read proves the key works. It does NOT prove that battery
    # control is enabled - that is a separate toggle in HEM.
    await api.async_get_status()


class HemConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user-facing setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect host, port and API key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_PORT] = int(user_input[CONF_PORT])
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
            )
            self._abort_if_unique_id_configured()

            try:
                await _async_validate(self.hass, user_input)
            except HemAuthError:
                errors["base"] = "invalid_auth"
            except HemError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - surface as a generic error
                _LOGGER.exception("Unexpected error validating HEM")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"Home Energy Manager ({user_input[CONF_HOST]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth - HEM shows a new key only once when it is regenerated."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a replacement API key."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            data = {**entry.data, CONF_API_KEY: user_input[CONF_API_KEY]}
            try:
                await _async_validate(self.hass, data)
            except HemAuthError:
                errors["base"] = "invalid_auth"
            except HemError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user move HEM to a new address or port."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            user_input[CONF_PORT] = int(user_input[CONF_PORT])
            try:
                await _async_validate(self.hass, user_input)
            except HemAuthError:
                errors["base"] = "invalid_auth"
            except HemError:
                errors["base"] = "cannot_connect"
            else:
                # The unique id *is* the address, so moving HEM necessarily
                # changes it. _abort_if_unique_id_mismatch() compares the new
                # id against this entry's own old one, which cannot match after
                # a move, so it rejected every move this step exists to make.
                # The real clash is a *different* entry already on that
                # address; nothing to check when the address has not changed.
                new_unique_id = f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
                if new_unique_id != entry.unique_id:
                    await self.async_set_unique_id(new_unique_id)
                    self._abort_if_unique_id_configured(error="wrong_device")
                return self.async_update_reload_and_abort(
                    entry, unique_id=new_unique_id, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, dict(entry.data)
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return HemOptionsFlow(config_entry)


class HemOptionsFlow(OptionsFlow):
    """Polling, control and presentation options."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Keep a private reference (avoids the deprecated self.config_entry set)."""
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the options."""
        if user_input is not None:
            # Selectors hand back floats; store ints so comparisons stay clean.
            for key in (
                CONF_SCAN_INTERVAL,
                CONF_FORCE_MINUTES,
                CONF_CONFIRM_TIMEOUT,
                CONF_DASHBOARD_PORT,
            ):
                user_input[key] = int(user_input[key])
            return self.async_create_entry(title="", data=user_input)

        options = self._entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=5,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_POLL_SNAPSHOT, default=options.get(CONF_POLL_SNAPSHOT, True)
                ): BooleanSelector(),
                vol.Required(
                    CONF_ENABLE_CONTROLS,
                    default=options.get(CONF_ENABLE_CONTROLS, False),
                ): BooleanSelector(),
                vol.Required(
                    CONF_FORCE_MINUTES,
                    default=options.get(CONF_FORCE_MINUTES, DEFAULT_FORCE_MINUTES),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_FORCE_MINUTES,
                        max=MAX_FORCE_MINUTES,
                        step=1,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_CONFIRM_TIMEOUT,
                    default=options.get(CONF_CONFIRM_TIMEOUT, DEFAULT_CONFIRM_TIMEOUT),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_CONFIRM_TIMEOUT,
                        max=MAX_CONFIRM_TIMEOUT,
                        step=10,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_DASHBOARD_PORT,
                    default=options.get(CONF_DASHBOARD_PORT, DEFAULT_DASHBOARD_PORT),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=65535, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
