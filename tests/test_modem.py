"""Tests for the Zyxel LTE7460 client.

Network is fully mocked via `responses`. Fixtures under ./fixtures/ are
real-shape JSON captured from a live LTE7460 (firmware V1.00(ABFR.4)C0)
with public IP addresses redacted. The disconnected and session-expired
fixtures are approximations — the parser/auth-flow assertions are kept
shape-agnostic enough to survive small differences in the real device.
"""

from __future__ import annotations

import pytest
import responses

from knausen_signal.modem import (
    ModemSample,
    ZyxelAuthError,
    ZyxelLockoutError,
    ZyxelLTE7460Client,
)

BASE = "https://192.168.1.1/cgi-bin/gui.cgi"


def make_client() -> ZyxelLTE7460Client:
    return ZyxelLTE7460Client("192.168.1.1", "admin", "secret")


def _match_action(action: str):
    """responses matcher: assert POST body has the given action."""
    import json as _json

    def matcher(request):
        body = _json.loads(request.body)
        ok = body.get("action") == action
        reason = "" if ok else f"expected action={action!r}, got {body.get('action')!r}"
        return ok, reason

    return matcher


# ---------- parser ----------

def test_parse_connected(fixture):
    body = fixture("wwan_status_connected.json")["get_wwan_network_internet_status"]
    sample = ZyxelLTE7460Client._parse_status(body)

    assert isinstance(sample, ModemSample)
    assert sample.connected is True
    assert sample.rsrp_dbm == -61
    assert sample.rsrq_db == -9
    assert sample.snr_db == 25
    assert sample.rssi_dbm == -31
    assert sample.cqi == -1
    assert sample.operator == "ice+"
    assert sample.mcc == 242
    assert sample.mnc == 14
    assert sample.network_type == "LTE+"
    # Carrier aggregation: primary + secondary both populated
    assert sample.band_primary == "E_UTRA_20"
    assert sample.earfcn_primary == 6200
    assert sample.band_secondary == "E_UTRA_3"
    assert sample.earfcn_secondary == 1850
    assert sample.tac == 20001
    assert sample.cid == 15129611
    assert sample.pci == 250
    assert sample.ipv4_address == "192.0.2.16"
    assert sample.ipv4_connection_time == "00:10:09:19"
    assert sample.ipv6_address == "2001:db8::1/64"


def test_parse_disconnected(fixture):
    body = fixture("wwan_status_disconnected.json")["get_wwan_network_internet_status"]
    sample = ZyxelLTE7460Client._parse_status(body)

    assert sample.connected is False
    assert sample.ipv4_address is None
    assert sample.operator is None
    assert sample.band_primary is None
    # Numeric fields the router reports as 0 when down — we pass them through;
    # the consumer can decide whether to ignore zeros from a disconnected sample.
    assert sample.rsrp_dbm == 0


def test_parse_handles_missing_lte_block():
    sample = ZyxelLTE7460Client._parse_status({"state": 0, "ip": "0.0.0.0"})
    assert sample.connected is False
    assert sample.rsrp_dbm is None
    assert sample.operator is None


# ---------- login ----------

@responses.activate
def test_login_success(fixture):
    responses.add(
        responses.POST, BASE,
        json=fixture("login_success.json"),
        match=[_match_action("set_system_user_login")],
    )
    client = make_client()
    client.login()
    assert client._logged_in is True


@responses.activate
def test_login_bad_password(fixture):
    responses.add(
        responses.POST, BASE,
        json=fixture("login_bad_password.json"),
        match=[_match_action("set_system_user_login")],
    )
    client = make_client()
    with pytest.raises(ZyxelAuthError):
        client.login()
    assert client._logged_in is False


@responses.activate
def test_login_lockout_carries_seconds(fixture):
    responses.add(
        responses.POST, BASE,
        json=fixture("login_lockout.json"),
        match=[_match_action("set_system_user_login")],
    )
    client = make_client()
    with pytest.raises(ZyxelLockoutError) as ei:
        client.login()
    assert ei.value.seconds_remaining == 300


# ---------- poll ----------

@responses.activate
def test_poll_logs_in_lazily_then_returns_sample(fixture):
    responses.add(
        responses.POST, BASE,
        json=fixture("login_success.json"),
        match=[_match_action("set_system_user_login")],
    )
    responses.add(
        responses.POST, BASE,
        json=fixture("wwan_status_connected.json"),
        match=[_match_action("get_wwan_network_internet_status")],
    )

    client = make_client()
    sample = client.poll()

    assert sample.connected is True
    assert sample.rsrp_dbm == -61
    # Two POSTs: one login, one status
    assert len(responses.calls) == 2


@responses.activate
def test_poll_relogins_on_session_expired(fixture):
    # 1: initial login OK
    responses.add(
        responses.POST, BASE,
        json=fixture("login_success.json"),
        match=[_match_action("set_system_user_login")],
    )
    # 2: status call hits an expired session (errno != 0)
    responses.add(
        responses.POST, BASE,
        json=fixture("wwan_status_session_expired.json"),
        match=[_match_action("get_wwan_network_internet_status")],
    )
    # 3: re-login OK
    responses.add(
        responses.POST, BASE,
        json=fixture("login_success.json"),
        match=[_match_action("set_system_user_login")],
    )
    # 4: status call succeeds
    responses.add(
        responses.POST, BASE,
        json=fixture("wwan_status_connected.json"),
        match=[_match_action("get_wwan_network_internet_status")],
    )

    client = make_client()
    sample = client.poll()

    assert sample.connected is True
    assert len(responses.calls) == 4


@responses.activate
def test_poll_does_not_recover_from_persistent_session_failure(fixture):
    """If even after re-login the call still errors, surface it."""
    responses.add(
        responses.POST, BASE,
        json=fixture("login_success.json"),
        match=[_match_action("set_system_user_login")],
    )
    responses.add(
        responses.POST, BASE,
        json=fixture("wwan_status_session_expired.json"),
        match=[_match_action("get_wwan_network_internet_status")],
    )
    responses.add(
        responses.POST, BASE,
        json=fixture("login_success.json"),
        match=[_match_action("set_system_user_login")],
    )
    responses.add(
        responses.POST, BASE,
        json=fixture("wwan_status_session_expired.json"),
        match=[_match_action("get_wwan_network_internet_status")],
    )

    from knausen_signal.modem import ZyxelResponseError
    client = make_client()
    with pytest.raises(ZyxelResponseError):
        client.poll()


@responses.activate
def test_http_401_clears_session_and_raises_auth_error():
    responses.add(
        responses.POST, BASE,
        status=401,
        json={"error": "unauthorized"},
        match=[_match_action("get_wwan_network_internet_status")],
    )
    client = make_client()
    client._logged_in = True  # pretend we had a session
    with pytest.raises(ZyxelAuthError):
        client._call("get_wwan_network_internet_status", {})
    assert client._logged_in is False
