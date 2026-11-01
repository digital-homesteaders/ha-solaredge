"""Config, reauth and options flows of the SolarEdge Cloud integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .api import (
    SolarEdgeAuthError,
    SolarEdgeCloudClient,
    SolarEdgeConnectionError,
    SolarEdgeError,
    SolarEdgeLoginRefusedError,
)
from .const import (
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .coordinator import SolarEdgeConfigEntry, async_create_session

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): USER_SCHEMA.schema[CONF_PASSWORD]})


async def _async_check_login(hass: HomeAssistant, username: str, password: str) -> tuple[str | None, list[dict[str, Any]]]:
    """Log in once and read the sites; the error key for the form, or None."""
    session = async_create_session(hass, auto_cleanup=False)
    try:
        client = SolarEdgeCloudClient(session, username, password)
        await client.login()
        return None, await client.get_sites()
    except SolarEdgeAuthError:
        return "invalid_auth", []
    except SolarEdgeLoginRefusedError as err:
        _LOGGER.warning("SolarEdge refused the login: %s", err)
        return "login_refused", []
    except SolarEdgeConnectionError:
        return "cannot_connect", []
    except SolarEdgeError as err:
        _LOGGER.debug("SolarEdge login check failed: %s", err)
        return "cannot_connect", []
    except Exception:
        _LOGGER.exception("Unexpected error checking the SolarEdge login")
        return "unknown", []
    finally:
        session.detach()


# voluptuous schemas: HA 2026.10 takes them through its probatio shim, and older
# versions (HACS users) need them.


class SolarEdgeCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a mySolarEdge account."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the mySolarEdge e-mail address and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            existing = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, username.lower())
            if existing is not None and existing.disabled_by is not None:
                # Hidden from the integrations list by default: say where it is.
                return self.async_abort(
                    reason="already_configured_disabled", description_placeholders={"title": existing.title}
                )
            self._abort_if_unique_id_configured()
            error, sites = await _async_check_login(self.hass, username, user_input[CONF_PASSWORD])
            if error:
                errors["base"] = error
            elif not sites:
                return self.async_abort(reason="no_sites")
            else:
                title = str(sites[0].get("name") or "mySolarEdge")
                if len(sites) > 1:
                    title = f"{title} (+{len(sites) - 1})"
                return self.async_create_entry(
                    title=title,
                    data={CONF_USERNAME: username, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[arg-type]
                USER_SCHEMA,  # type: ignore[arg-type]
                {CONF_USERNAME: (user_input or {}).get(CONF_USERNAME)},
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """SolarEdge rejected the stored password."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            error, _ = await _async_check_login(
                self.hass, entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,  # type: ignore[arg-type]
            errors=errors,
            description_placeholders={"title": entry.title},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: SolarEdgeConfigEntry) -> SolarEdgeCloudOptionsFlow:
        """The poll interval."""
        return SolarEdgeCloudOptionsFlow()


class SolarEdgeCloudOptionsFlow(OptionsFlow):
    """Change the poll interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The live power poll interval in seconds."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL])}
            )
        schema = vol.Schema(
            {
                vol.Required(CONF_POLL_INTERVAL): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_POLL_INTERVAL,
                        max=MAX_POLL_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[arg-type]
                schema,  # type: ignore[arg-type]
                {
                    CONF_POLL_INTERVAL: self.config_entry.options.get(
                        CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                    )
                },
            ),
        )
