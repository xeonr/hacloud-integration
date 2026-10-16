"""Constants for the Xeonr Home Assistant cloud integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "xeonr_cloud"

# The instance API. Connect JSON at <API_BASE_URL>/<package>.<Service>/<Method>.
# hacloud-infra's ingress (stage: https://hacloud.xeonr.dev).
DEFAULT_API_BASE_URL: Final = "https://hacloud.xeonr.io"

# Xeonr Auth (stage: https://auth.xeonr.dev). The public "Home Assistant"
# device-flow client, made by platform-docs console/docs/hacloud-portal-saves.sh.
DEFAULT_AUTH_ISSUER: Final = "https://auth.xeonr.io"
DEFAULT_CLIENT_ID: Final = "2ced5235-f72a-4ed9-aca0-023bee71b8ac"
# CreateInstance exchanges this token (RFC 8693) for Burrow and upl tokens,
# so it must carry their delegated scopes as well as our own. No
# offline_access: the token is used once and dropped.
AUTH_SCOPES: Final = (
    "openid",
    "profile",
    "auth:teams:read",
    "hacloud:instances:read",
    "hacloud:instances:edit",
    "burrow:managed:read",
    "burrow:managed:edit",
    "upl:bucket:read",
    "upl:bucket:manage",
)

# Where the person manages instances.
CONSOLE_URL: Final = "https://console.xeonr.io/hacloud"

# Config entry data. Only the instance credential is kept, never the
# person's tokens.
CONF_INSTANCE_ID: Final = "instance_id"
CONF_INSTANCE_SECRET: Final = "instance_secret"
CONF_TEAM: Final = "team"
CONF_API_BASE_URL: Final = "api_base_url"

# Re-auth data flag: link a new instance rather than regenerate the
# credential of the existing one (its reservation is gone, HAC-4).
REAUTH_RELINK_NEW: Final = "relink_new_instance"

# Options.
CONF_SET_EXTERNAL_URL: Final = "set_external_url"
# The URL we set as external_url, so we only ever clear our own.
CONF_EXTERNAL_URL_SET_BY_US: Final = "external_url_set"

# MarkConnectionSeen: on start, every connect and disconnect, and at least
# every five minutes. Four leaves room for a slow call.
SEEN_INTERVAL: Final = timedelta(minutes=4)

# How often to re-ask GetConnection while remote access is off or the
# reservation is gone, so turning it back on in the console heals by itself.
PRECONDITION_RECHECK: Final = timedelta(minutes=5)

# Agent restart backoff.
RECONNECT_MIN_SECONDS: Final = 5
RECONNECT_MAX_SECONDS: Final = 300

# ICE credentials are refreshed this long before expires_at, or at half their
# lifetime if that is shorter.
ICE_REFRESH_MARGIN: Final = timedelta(minutes=10)
ICE_RETRY: Final = timedelta(minutes=1)
# With no expires_at from the server, refresh this often.
ICE_DEFAULT_REFRESH: Final = timedelta(hours=1)

CONNECT_TIMEOUT: Final = 30

# Repair issue ids (suffixed with the entry id).
ISSUE_REMOTE_ACCESS_DISABLED: Final = "remote_access_disabled"
ISSUE_RESERVATION_REMOVED: Final = "reservation_removed"
ISSUE_INSTANCE_UNLINKED: Final = "instance_unlinked"
ISSUE_AGENT_UNAVAILABLE: Final = "agent_unavailable"

SIGNAL_CONNECTION_STATE: Final = f"{DOMAIN}_connection_state_{{}}"
