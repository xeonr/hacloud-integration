"""The Connect JSON client's parsing, without Home Assistant."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.xeonr_cloud.api import ConnectError, _parse_time
from custom_components.xeonr_cloud.auth import teams_from_token

from .fake_cloud import error_info


def test_error_info_reason_from_binary_value() -> None:
    err = ConnectError("failed_precondition", "x", [error_info("RESERVATION_REMOVED")])
    assert err.reason == "RESERVATION_REMOVED"


def test_error_info_reason_prefers_debug() -> None:
    err = ConnectError(
        "failed_precondition",
        "x",
        [
            {
                "type": "google.rpc.ErrorInfo",
                "value": "!!",
                "debug": {"reason": "REMOTE_ACCESS_DISABLED"},
            }
        ],
    )
    assert err.reason == "REMOTE_ACCESS_DISABLED"


def test_error_without_details() -> None:
    assert ConnectError("failed_precondition").reason is None
    assert (
        ConnectError("failed_precondition", details=[{"type": "other"}]).reason is None
    )
    assert (
        ConnectError(
            "failed_precondition",
            details=[{"type": "google.rpc.ErrorInfo", "value": "%%"}],
        ).reason
        is None
    )


def test_go_timestamps() -> None:
    assert _parse_time("2026-10-16T12:00:00.123456789Z") == datetime(
        2026, 10, 16, 12, 0, 0, 123456, tzinfo=UTC
    )
    assert _parse_time("2026-10-16T12:00:00Z") == datetime(2026, 10, 16, 12, tzinfo=UTC)
    assert _parse_time("") is None


def test_teams_claim() -> None:
    import base64
    import json

    claims = {
        "urn:xeonr:auth:teams": [
            {"id": "urn:xeonr:auth:team:1", "slug": "home", "roles": ["editor"]}
        ]
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    [team] = teams_from_token(f"h.{payload}.s")
    assert team.urn == "urn:xeonr:auth:team:1"
    assert team.application_roles == ("editor",)
    assert teams_from_token("not-a-jwt") == []
