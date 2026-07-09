"""Tests for the Zyxel LTE7460 client.

Transport is injected — production uses `ssh + JsonClient` against a Unix
socket on the router, tests use a stub that returns fixture dicts. The
fixtures under ./fixtures/ are real-shape JSON captured from a live
LTE7460 (firmware V1.00(ABFR.4)C0), same schema either way.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from knausen_signal.modem import (
    ModemSample,
    ZyxelLTE7460Client,
    ZyxelResponseError,
    ZyxelTransportError,
    _parse_jsonclient_reply,
)


def make_client(transport=None) -> ZyxelLTE7460Client:
    return ZyxelLTE7460Client(
        "192.168.1.1",
        "/dev/null",           # ssh_key_path, unused when transport is injected
        transport=transport,
    )


# ---------- _parse_status ----------

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
    assert sample.rsrp_dbm == 0


def test_parse_handles_missing_lte_block():
    sample = ZyxelLTE7460Client._parse_status({"state": 0, "ip": "0.0.0.0"})
    assert sample.connected is False
    assert sample.rsrp_dbm is None
    assert sample.operator is None


# ---------- poll (via injected transport) ----------

def _build_transport(fixture):
    """Return a transport that answers the three RPCs from fixture files."""
    responses = {
        "get_wwan_network_internet_status":
            fixture("wwan_status_connected.json")["get_wwan_network_internet_status"],
        "get_wwan_pkt_threshold":
            fixture("wwan_pkt_threshold.json")["get_wwan_pkt_threshold"],
        "get_wwan_total_network_stats":
            fixture("wwan_total_network_stats.json")["get_wwan_total_network_stats"],
    }
    calls: list[tuple[str, dict]] = []

    def transport(action, args):
        calls.append((action, args))
        return responses[action]

    transport.calls = calls
    return transport


def test_poll_calls_three_rpcs_and_returns_full_sample(fixture):
    transport = _build_transport(fixture)
    client = make_client(transport=transport)

    sample = client.poll()

    assert sample.connected is True
    assert sample.rsrp_dbm == -61
    # KiB from router * 1024 = true bytes
    assert sample.data_usage_tx_bytes == 10485760 * 1024
    assert sample.data_usage_rx_bytes == 100000000 * 1024
    # Order: status first, then threshold (for cycle dates), then stats
    assert [c[0] for c in transport.calls] == [
        "get_wwan_network_internet_status",
        "get_wwan_pkt_threshold",
        "get_wwan_total_network_stats",
    ]


def test_poll_forwards_cycle_dates_from_threshold_to_stats(fixture):
    transport = _build_transport(fixture)
    make_client(transport=transport).poll()

    stats_call = next(c for c in transport.calls if c[0] == "get_wwan_total_network_stats")
    assert stats_call[1] == {"start_date": "0701", "end_date": "0731"}


def test_poll_tolerates_data_usage_failure_and_still_returns_sample(fixture):
    """Usage endpoints failing must not drop the primary modem sample."""
    def transport(action, args):
        if action == "get_wwan_network_internet_status":
            return fixture("wwan_status_connected.json")["get_wwan_network_internet_status"]
        raise ZyxelResponseError(action, 1, "boom")

    sample = make_client(transport=transport).poll()
    assert sample.connected is True
    assert sample.rsrp_dbm == -61
    assert sample.data_usage_tx_bytes is None
    assert sample.data_usage_rx_bytes is None


def test_poll_data_usage_none_when_threshold_lacks_cycle(fixture):
    def transport(action, args):
        if action == "get_wwan_network_internet_status":
            return fixture("wwan_status_connected.json")["get_wwan_network_internet_status"]
        if action == "get_wwan_pkt_threshold":
            return {"errno": 0, "usage_cycle": {}}       # missing start/end dates
        raise AssertionError(f"stats should not have been called; got {action}")

    sample = make_client(transport=transport).poll()
    assert sample.data_usage_tx_bytes is None
    assert sample.data_usage_rx_bytes is None


def test_status_transport_errors_propagate(fixture):
    def transport(action, args):
        raise ZyxelTransportError("ssh timeout")

    with pytest.raises(ZyxelTransportError):
        make_client(transport=transport).poll()


# ---------- _parse_jsonclient_reply ----------

REAL_REPLY = (
    "send:\n"
    '{ "action": "get_wwan_network_internet_status" }\n'
    "read:\n"
    '{ "get_wwan_network_internet_status": '
    '{ "errno": 0, "errmsg": "", "state": 3, "ip": "1.2.3.4", '
    '"lte": {"rsrp": -61, "operator": "ice+"} } }\n'
)


def test_parse_jsonclient_reply_extracts_action_body():
    body = _parse_jsonclient_reply(REAL_REPLY, "get_wwan_network_internet_status")
    assert body["errno"] == 0
    assert body["ip"] == "1.2.3.4"
    assert body["lte"]["rsrp"] == -61


def test_parse_jsonclient_reply_rejects_missing_read_marker():
    with pytest.raises(ZyxelTransportError, match="lacked 'read:' marker"):
        _parse_jsonclient_reply("send:\n{...}\n", "any_action")


def test_parse_jsonclient_reply_rejects_invalid_json():
    with pytest.raises(ZyxelTransportError, match="not valid JSON"):
        _parse_jsonclient_reply("read:\nnot json at all", "x")


def test_parse_jsonclient_reply_rejects_missing_inner_object():
    reply = 'read:\n{ "some_other_action": {} }\n'
    with pytest.raises(ZyxelTransportError, match="lacked expected"):
        _parse_jsonclient_reply(reply, "get_wwan_network_internet_status")


# ---------- _ssh_transport (subprocess mocked) ----------

def _fake_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def test_ssh_transport_success_returns_body_and_shell_quotes_args():
    client = make_client()
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _fake_completed(stdout=REAL_REPLY)

    with patch("knausen_signal.modem.subprocess.run", side_effect=fake_run):
        body = client._ssh_transport("get_wwan_network_internet_status", {})

    assert body["ip"] == "1.2.3.4"
    argv = captured["argv"]
    assert argv[0] == "ssh"
    assert "admin@192.168.1.1" in argv
    # No args passed: remote command has no trailing JSON.
    assert argv[-1] == "JsonClient /dev/shm/cgi-2-sys get_wwan_network_internet_status"


def test_ssh_transport_passes_json_args_single_quoted():
    client = make_client()
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _fake_completed(stdout=(
            "read:\n"
            '{ "get_wwan_total_network_stats": '
            '{ "errno": 0, "tx": 1, "rx": 2 } }\n'
        ))

    with patch("knausen_signal.modem.subprocess.run", side_effect=fake_run):
        client._ssh_transport(
            "get_wwan_total_network_stats",
            {"start_date": "0701", "end_date": "0731"},
        )

    remote_cmd = captured["argv"][-1]
    # Args go after the action name, single-quoted so the remote shell
    # doesn't interpret the JSON braces / quotes.
    assert remote_cmd.startswith(
        "JsonClient /dev/shm/cgi-2-sys get_wwan_total_network_stats "
    )
    assert remote_cmd.endswith("'")
    assert '"start_date": "0701"' in remote_cmd
    assert '"end_date": "0731"' in remote_cmd


def test_ssh_transport_nonzero_exit_raises_transport_error():
    client = make_client()

    def fake_run(argv, **kwargs):
        return _fake_completed(returncode=255, stderr="Permission denied (publickey).")

    with patch("knausen_signal.modem.subprocess.run", side_effect=fake_run):
        with pytest.raises(ZyxelTransportError, match="Permission denied"):
            client._ssh_transport("x", {})


def test_ssh_transport_timeout_raises_transport_error():
    client = make_client()

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    with patch("knausen_signal.modem.subprocess.run", side_effect=fake_run):
        with pytest.raises(ZyxelTransportError, match="timed out"):
            client._ssh_transport("x", {})


def test_ssh_transport_errno_nonzero_raises_response_error():
    client = make_client()

    def fake_run(argv, **kwargs):
        return _fake_completed(stdout=(
            "read:\n"
            '{ "get_x": { "errno": 42, "errmsg": "no such action" } }\n'
        ))

    with patch("knausen_signal.modem.subprocess.run", side_effect=fake_run):
        with pytest.raises(ZyxelResponseError) as ei:
            client._ssh_transport("get_x", {})
    assert ei.value.errno == 42
    assert ei.value.action == "get_x"
