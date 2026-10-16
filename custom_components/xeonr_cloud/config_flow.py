"""Config flow: Xeonr Auth device sign-in, team pick, CreateInstance."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
import voluptuous as vol

from .api import ConnectError, HacloudError, HacloudUserApi
from .auth import AuthError, DeviceAuthorization, Team, XeonrAuthClient
from .const import (
    AUTH_SCOPES,
    CONF_API_BASE_URL,
    CONF_EXTERNAL_URL_SET_BY_US,
    CONF_INSTANCE_ID,
    CONF_INSTANCE_SECRET,
    CONF_SET_EXTERNAL_URL,
    CONF_TEAM,
    CONSOLE_URL,
    DOMAIN,
)
from .settings import async_get_settings, user_agent

_LOGGER = logging.getLogger(__name__)


class XeonrCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Link this Home Assistant to a Xeonr team."""

    VERSION = 1

    def __init__(self) -> None:
        self._auth: XeonrAuthClient | None = None
        self._device: DeviceAuthorization | None = None
        self._login_task: asyncio.Task[str] | None = None
        self._login_error: str | None = None
        # Held in memory for the length of the flow only.
        self._access_token: str | None = None
        self._teams: list[Team] = []
        self._team: Team | None = None
        self._entry_data: dict[str, Any] | None = None
        self._entry_title = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return XeonrCloudOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is None:
            return self.async_show_form(
                step_id="user", description_placeholders={"console_url": CONSOLE_URL}
            )
        return await self.async_step_device()

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_device()

    # Sign-in

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the code and link, and poll in the background."""
        if self._device is None:
            settings = async_get_settings(self.hass)
            self._auth = XeonrAuthClient(
                async_get_clientsession(self.hass),
                settings.auth_issuer,
                settings.client_id,
                AUTH_SCOPES,
            )
            try:
                self._device = await self._auth.start()
            except AuthError as err:
                _LOGGER.warning("Device sign-in could not start: %s", err)
                if err.code in ("invalid_client", "unauthorized_client"):
                    return self.async_abort(reason="invalid_client")
                return self.async_abort(reason="cannot_connect")

        if self._login_task is None:
            self._login_task = self.hass.async_create_task(
                self._async_wait_for_login(), eager_start=False
            )

        if self._login_task.done():
            return self.async_show_progress_done(
                next_step_id="signed_in" if self._access_token else "sign_in_failed"
            )

        return self.async_show_progress(
            step_id="device",
            progress_action="wait_for_device",
            description_placeholders={
                "url": self._device.verification_uri_complete,
                "code": self._device.user_code,
            },
            progress_task=self._login_task,
        )

    async def _async_wait_for_login(self) -> str:
        assert self._auth is not None and self._device is not None
        try:
            self._access_token = await self._auth.poll(self._device)
        except AuthError as err:
            _LOGGER.info("Device sign-in ended: %s", err)
            self._login_error = err.code
            return ""
        return self._access_token

    async def async_step_sign_in_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        reason = {
            "access_denied": "access_denied",
            "expired_token": "code_expired",
            "cannot_connect": "cannot_connect",
        }.get(self._login_error or "", "sign_in_failed")
        return self.async_abort(reason=reason)

    async def async_step_signed_in(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._auth is not None and self._access_token is not None
        self._teams = sorted(
            await self._auth.list_teams(self._access_token),
            key=lambda t: t.label.casefold(),
        )
        if not self._teams:
            # An empty team lets the API pick the caller's only team, and
            # refuse with a clear error otherwise.
            return await self._async_create_instance(team="")

        if self.source == SOURCE_REAUTH:
            previous = self._get_reauth_entry().data.get(CONF_TEAM)
            for team in self._teams:
                if previous in (team.urn, team.slug):
                    return await self._async_create_instance(team=team.urn)

        if len(self._teams) == 1:
            return await self._async_create_instance(team=self._teams[0].urn)
        return await self.async_step_team()

    async def async_step_team(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self._async_create_instance(team=user_input[CONF_TEAM])
        return self.async_show_form(
            step_id="team",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TEAM): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": t.urn, "label": t.label} for t in self._teams
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # Link

    async def _async_create_instance(self, *, team: str) -> ConfigFlowResult:
        assert self._access_token is not None
        settings = async_get_settings(self.hass)
        api = HacloudUserApi(
            async_get_clientsession(self.hass),
            settings.api_base_url,
            self._access_token,
            user_agent(self.hass),
        )
        name = (self.hass.config.location_name or "Home Assistant")[:64]
        try:
            created = await api.create_instance(
                team=team,
                name=name,
                home_assistant_version=HA_VERSION,
                integration_version=await self._async_integration_version(),
            )
        except ConnectError as err:
            _LOGGER.warning("CreateInstance refused: %s", err)
            return self.async_abort(
                reason={
                    "resource_exhausted": "instance_limit",
                    "failed_precondition": "product_not_enabled",
                    "permission_denied": "not_editor",
                    "unauthenticated": "sign_in_failed",
                }.get(err.code, "cannot_connect"),
                description_placeholders={"console_url": CONSOLE_URL},
            )
        except HacloudError as err:
            _LOGGER.warning("CreateInstance failed: %s", err)
            return self.async_abort(reason="cannot_connect")
        finally:
            # The person's token has done its job.
            self._access_token = None

        self._entry_title = created.instance.name or name
        self._entry_data = {
            CONF_INSTANCE_ID: created.instance.id,
            CONF_INSTANCE_SECRET: created.instance_secret,
            CONF_TEAM: created.instance.team or team,
            CONF_API_BASE_URL: settings.api_base_url,
        }

        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            data = dict(self._entry_data)
            if CONF_EXTERNAL_URL_SET_BY_US in entry.data:
                data[CONF_EXTERNAL_URL_SET_BY_US] = entry.data[
                    CONF_EXTERNAL_URL_SET_BY_US
                ]
            return self.async_update_reload_and_abort(
                entry, unique_id=created.instance.id, data=data
            )

        await self.async_set_unique_id(created.instance.id)
        self._abort_if_unique_id_configured()

        if self.hass.config.external_url is None:
            return await self.async_step_external_url()
        return self._async_finish(set_external_url=False)

    async def async_step_external_url(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the tunnel as external_url, only while none is set."""
        if user_input is not None:
            return self._async_finish(
                set_external_url=user_input[CONF_SET_EXTERNAL_URL]
            )
        return self.async_show_form(
            step_id="external_url",
            data_schema=vol.Schema(
                {vol.Required(CONF_SET_EXTERNAL_URL, default=True): bool}
            ),
        )

    @callback
    def _async_finish(self, *, set_external_url: bool) -> ConfigFlowResult:
        assert self._entry_data is not None
        return self.async_create_entry(
            title=self._entry_title,
            data=self._entry_data,
            options={CONF_SET_EXTERNAL_URL: set_external_url},
        )

    async def _async_integration_version(self) -> str:
        from homeassistant.loader import async_get_integration

        integration = await async_get_integration(self.hass, DOMAIN)
        return str(integration.version or "")


class XeonrCloudOptionsFlow(OptionsFlow):
    """Offer external_url only when it is unset, or still the one we set."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = self.hass.config.external_url
        ours = self.config_entry.data.get(CONF_EXTERNAL_URL_SET_BY_US)
        if current is not None and current != ours:
            return self.async_abort(
                reason="external_url_set_by_user",
                description_placeholders={"external_url": current},
            )
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **self.config_entry.options,
                    CONF_SET_EXTERNAL_URL: user_input[CONF_SET_EXTERNAL_URL],
                }
            )
        default = self.config_entry.options.get(CONF_SET_EXTERNAL_URL, True)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_SET_EXTERNAL_URL, default=default): bool}
            ),
        )
