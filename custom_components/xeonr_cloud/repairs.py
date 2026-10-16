"""Repair issues, and the fix flow that relinks an unlinked instance."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONSOLE_URL,
    DOMAIN,
    ISSUE_AGENT_UNAVAILABLE,
    ISSUE_INSTANCE_UNLINKED,
    ISSUE_REMOTE_ACCESS_DISABLED,
    ISSUE_RESERVATION_REMOVED,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_CONNECTION_ISSUES = (
    ISSUE_REMOTE_ACCESS_DISABLED,
    ISSUE_RESERVATION_REMOVED,
    ISSUE_AGENT_UNAVAILABLE,
)


def issue_id(kind: str, entry: ConfigEntry) -> str:
    return f"{kind}_{entry.entry_id}"


@callback
def async_raise_precondition(
    hass: HomeAssistant, entry: ConfigEntry, *, reservation_removed: bool
) -> None:
    """Remote access switched off, or the reservation deleted in Burrow."""
    if reservation_removed:
        ir.async_delete_issue(
            hass, DOMAIN, issue_id(ISSUE_REMOTE_ACCESS_DISABLED, entry)
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(ISSUE_RESERVATION_REMOVED, entry),
            is_fixable=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_RESERVATION_REMOVED,
            translation_placeholders={"title": entry.title, "console_url": CONSOLE_URL},
            data={"entry_id": entry.entry_id},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id(ISSUE_RESERVATION_REMOVED, entry))
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(ISSUE_REMOTE_ACCESS_DISABLED, entry),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_REMOTE_ACCESS_DISABLED,
            translation_placeholders={"title": entry.title, "console_url": CONSOLE_URL},
            learn_more_url=CONSOLE_URL,
        )


@callback
def async_raise_agent_unavailable(
    hass: HomeAssistant, entry: ConfigEntry, error: str
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(ISSUE_AGENT_UNAVAILABLE, entry),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_AGENT_UNAVAILABLE,
        translation_placeholders={"title": entry.title, "error": error},
    )


@callback
def async_clear_connection_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    for kind in _CONNECTION_ISSUES:
        ir.async_delete_issue(hass, DOMAIN, issue_id(kind, entry))


@callback
def async_raise_unlinked(
    hass: HomeAssistant, entry: ConfigEntry, *, start_reauth: bool = True
) -> None:
    """The instance credential was refused: the instance was unlinked in the
    console, or its credential revoked. Raise the issue and start re-auth,
    which relinks."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(ISSUE_INSTANCE_UNLINKED, entry),
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_INSTANCE_UNLINKED,
        translation_placeholders={"title": entry.title, "console_url": CONSOLE_URL},
        data={"entry_id": entry.entry_id},
    )
    if start_reauth:
        entry.async_start_reauth(hass)


@callback
def async_clear_unlinked(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, issue_id(ISSUE_INSTANCE_UNLINKED, entry))


class RelinkRepairFlow(RepairsFlow):
    """Confirm, then hand over to the re-auth flow, which relinks."""

    def __init__(self, entry_id: str | None) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> Any:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> Any:
        if user_input is None:
            return self.async_show_form(step_id="confirm")
        entry = (
            self.hass.config_entries.async_get_entry(self._entry_id)
            if self._entry_id
            else None
        )
        if entry is not None:
            entry.async_start_reauth(self.hass)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    entry_id = data.get("entry_id") if data else None
    return RelinkRepairFlow(str(entry_id) if entry_id else None)
