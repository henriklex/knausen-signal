"""Tests for the push worker (DB drain + series projection + remote_write).

Network is mocked. SQLite uses an in-memory file per test via a tmp_path
fixture so the on-disk schema in db.py is exercised end-to-end.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from knausen_signal.db import (
    insert_modem_sample,
    insert_mtr_snapshot,
    insert_probe_sample,
    open_db,
)
from knausen_signal.push import (
    _modem_metrics,
    _mtr_metrics,
    _probe_metrics,
    build_series,
    push_unpushed,
)
from knausen_signal.remote_write import Label

# A representative modem payload with carrier aggregation active and
# monthly data-usage populated.
MODEM_PAYLOAD = {
    "connected": True,
    "rsrp_dbm": -61, "rsrq_db": -9, "snr_db": 25, "rssi_dbm": -31, "cqi": -1,
    "operator": "ice+", "mcc": 242, "mnc": 14, "network_type": "LTE+",
    "band_primary": "E_UTRA_20", "earfcn_primary": 6200,
    "band_secondary": "E_UTRA_3", "earfcn_secondary": 1850,
    "tac": 20001, "cid": 15129611, "pci": 250,
    "ipv4_address": "100.121.166.16",
    "ipv4_connection_time": "00:10:09:19",
    "ipv6_address": "::1/64",
    "data_usage_tx_bytes": 12345678901,
    "data_usage_rx_bytes": 102400000000,
}

PROBE_PAYLOAD = {
    "ping_rtt_ms_p50": 55.5, "ping_rtt_ms_p95": 108.3, "ping_loss_pct": 0.0,
    "dns_lookup_ms": 168.0, "tcp_connect_ms": 51.4,
    "tls_handshake_ms": 65.7, "https_head_ms": 166.7,
    "probe_ok": True,
}


# ---------- _modem_metrics ----------

def test_modem_metrics_yields_all_numeric_gauges_plus_derived():
    out = list(_modem_metrics(MODEM_PAYLOAD))
    names = {name for name, _, _ in out}
    # Numeric gauges
    for expected in (
        "knausen_modem_rsrp_dbm", "knausen_modem_rsrq_db", "knausen_modem_snr_db",
        "knausen_modem_rssi_dbm", "knausen_modem_cqi",
        "knausen_modem_pci", "knausen_modem_cid", "knausen_modem_tac",
        "knausen_modem_mcc", "knausen_modem_mnc",
        "knausen_modem_earfcn_primary", "knausen_modem_earfcn_secondary",
        "knausen_modem_connected", "knausen_modem_carrier_aggregation",
        "knausen_modem_info",
        "knausen_modem_data_usage_tx_bytes", "knausen_modem_data_usage_rx_bytes",
        "knausen_modem_data_usage_total_bytes",
        # _gb variants exist because vmui custom dashboards can't do
        # arithmetic inside a panel's PromQL expr.
        "knausen_modem_data_usage_tx_gb", "knausen_modem_data_usage_rx_gb",
        "knausen_modem_data_usage_total_gb",
    ):
        assert expected in names, f"missing {expected}"


def test_modem_metrics_data_usage_total_is_sum_of_tx_and_rx():
    tx = MODEM_PAYLOAD["data_usage_tx_bytes"]
    rx = MODEM_PAYLOAD["data_usage_rx_bytes"]
    assert _find(MODEM_PAYLOAD, "knausen_modem_data_usage_total_bytes") == \
        pytest.approx(tx + rx)
    # _gb variants are just the bytes / 1024^3
    assert _find(MODEM_PAYLOAD, "knausen_modem_data_usage_total_gb") == \
        pytest.approx((tx + rx) / (1024 ** 3))
    assert _find(MODEM_PAYLOAD, "knausen_modem_data_usage_tx_gb") == \
        pytest.approx(tx / (1024 ** 3))
    assert _find(MODEM_PAYLOAD, "knausen_modem_data_usage_rx_gb") == \
        pytest.approx(rx / (1024 ** 3))


def test_modem_metrics_data_usage_omitted_when_either_side_is_none():
    """A half-populated usage payload never fabricates a fake total."""
    partial = {**MODEM_PAYLOAD, "data_usage_rx_bytes": None}
    names = {name for name, _, _ in _modem_metrics(partial)}
    assert "knausen_modem_data_usage_tx_bytes" in names
    assert "knausen_modem_data_usage_tx_gb" in names
    assert "knausen_modem_data_usage_rx_bytes" not in names
    assert "knausen_modem_data_usage_rx_gb" not in names
    assert "knausen_modem_data_usage_total_bytes" not in names
    assert "knausen_modem_data_usage_total_gb" not in names


def test_modem_metrics_carrier_aggregation_flag_flips_with_band_secondary():
    no_ca = {**MODEM_PAYLOAD, "band_secondary": None, "earfcn_secondary": None}
    assert _find(MODEM_PAYLOAD, "knausen_modem_carrier_aggregation") == 1.0
    assert _find(no_ca,         "knausen_modem_carrier_aggregation") == 0.0


def test_modem_metrics_skips_none_numeric_fields():
    payload = {"connected": False, "rsrp_dbm": None, "rsrq_db": -8}
    yielded = {name: value for name, _, value in _modem_metrics(payload)}
    assert "knausen_modem_rsrp_dbm" not in yielded
    assert yielded["knausen_modem_rsrq_db"] == -8.0
    assert yielded["knausen_modem_connected"] == 0.0


def test_modem_info_labels_carry_string_context():
    info = next(
        (labs for name, labs, _ in _modem_metrics(MODEM_PAYLOAD)
         if name == "knausen_modem_info"),
        None,
    )
    assert info is not None
    asdict = {l.name: l.value for l in info}
    assert asdict == {
        "operator": "ice+",
        "network_type": "LTE+",
        "band_primary": "E_UTRA_20",
        "band_secondary": "E_UTRA_3",
        "band_primary_name": "800 DD",
        "band_secondary_name": "1800+",
    }


def test_band_name_maps_known_and_passes_through_unknown():
    from knausen_signal.push import _band_name
    assert _band_name("E_UTRA_3") == "1800+"
    assert _band_name("E_UTRA_20") == "800 DD"
    assert _band_name("E_UTRA_7") == "E_UTRA_7"   # unmapped -> raw, never hidden
    assert _band_name(None) == ""
    assert _band_name("") == ""


# ---------- _probe_metrics ----------

def test_probe_metrics_yields_all_plus_ok_flag():
    yielded = {name: value for name, _, value in _probe_metrics(PROBE_PAYLOAD)}
    assert yielded["knausen_probe_ping_rtt_ms_p50"] == pytest.approx(55.5)
    assert yielded["knausen_probe_ok"] == 1.0


def test_probe_metrics_skips_none_subprobes():
    yielded = {
        name: value
        for name, _, value in _probe_metrics({
            **PROBE_PAYLOAD, "tls_handshake_ms": None, "https_head_ms": None,
        })
    }
    assert "knausen_probe_tls_handshake_ms" not in yielded
    assert "knausen_probe_https_head_ms" not in yielded
    assert yielded["knausen_probe_ok"] == 1.0


# ---------- checkpoint projection ----------

def test_probe_metrics_emits_per_checkpoint_with_label():
    payload = {
        **PROBE_PAYLOAD,
        "checkpoint_rtt_ms_p50": {"gateway": 1.5, "carrier_edge": 40.0},
        "checkpoint_rtt_ms_p95": {"gateway": 2.0, "carrier_edge": 180.0},
        "checkpoint_loss_pct":   {"gateway": 0.0, "carrier_edge": 0.0},
    }
    emitted = list(_probe_metrics(payload))
    cp_p50 = [
        (labs, val) for name, labs, val in emitted
        if name == "knausen_probe_checkpoint_rtt_ms_p50"
    ]
    by_checkpoint = {
        next(l.value for l in labs if l.name == "checkpoint"): val
        for labs, val in cp_p50
    }
    assert by_checkpoint == {"gateway": 1.5, "carrier_edge": 40.0}


def test_probe_metrics_skips_none_checkpoint_values():
    payload = {
        **PROBE_PAYLOAD,
        "checkpoint_rtt_ms_p50": {"gateway": 1.5, "carrier_edge": None},
        "checkpoint_rtt_ms_p95": {},
        "checkpoint_loss_pct":   {"gateway": 0.0, "carrier_edge": None},
    }
    emitted_names = {
        (name, next((l.value for l in labs if l.name == "checkpoint"), None))
        for name, labs, _ in _probe_metrics(payload)
        if name.startswith("knausen_probe_checkpoint_")
    }
    # carrier_edge None entries dropped; gateway present
    assert ("knausen_probe_checkpoint_rtt_ms_p50", "gateway") in emitted_names
    assert ("knausen_probe_checkpoint_rtt_ms_p50", "carrier_edge") not in emitted_names
    assert ("knausen_probe_checkpoint_loss_pct", "gateway") in emitted_names


def test_probe_metrics_empty_checkpoints_emits_no_checkpoint_series():
    """Backward compat: payloads without checkpoint keys still work."""
    emitted = list(_probe_metrics(PROBE_PAYLOAD))
    assert not any(
        name.startswith("knausen_probe_checkpoint_")
        for name, _, _ in emitted
    )


# ---------- mtr projection ----------

MTR_PAYLOAD = {
    "target": "8.8.8.8",
    "started_at": 1000.0,
    "hops": [
        {"hop_num": 1, "host": "192.168.1.1", "loss_pct": 0.0, "sent": 30,
         "rtt_last": 1.5, "rtt_avg": 2.0, "rtt_best": 1.4, "rtt_worst": 3.0,
         "rtt_stdev": 0.4},
        {"hop_num": 4, "host": "10.4.208.17", "loss_pct": 0.0, "sent": 30,
         "rtt_last": 88.8, "rtt_avg": 79.3, "rtt_best": 28.6, "rtt_worst": 197.2,
         "rtt_stdev": 39.5},
    ],
}


def test_mtr_metrics_yields_five_data_metrics_per_hop_plus_info():
    emitted = list(_mtr_metrics(MTR_PAYLOAD))
    names = [name for name, _, _ in emitted]
    # 5 data + 1 info per hop, 2 hops = 12 series
    assert len(emitted) == 12
    assert names.count("knausen_mtr_hop_rtt_ms_worst") == 2
    assert names.count("knausen_mtr_hop_info") == 2


def test_mtr_metrics_data_series_labelled_target_and_hop_num_only():
    """host stays out of data labels — keeps cardinality stable on route flip."""
    for name, labs, _ in _mtr_metrics(MTR_PAYLOAD):
        if name == "knausen_mtr_hop_info":
            continue
        label_names = {l.name for l in labs}
        assert label_names == {"target", "hop_num"}, \
            f"{name} unexpectedly carried labels {label_names}"


def test_mtr_metrics_info_series_carries_host():
    info = [
        labs for name, labs, _ in _mtr_metrics(MTR_PAYLOAD)
        if name == "knausen_mtr_hop_info"
    ]
    hosts_by_hop = {
        next(l.value for l in labs if l.name == "hop_num"):
        next(l.value for l in labs if l.name == "host")
        for labs in info
    }
    assert hosts_by_hop == {"1": "192.168.1.1", "4": "10.4.208.17"}


def test_mtr_metrics_empty_hops_yields_nothing():
    assert list(_mtr_metrics({"target": "x", "started_at": 0, "hops": []})) == []


# ---------- build_series ----------

def test_build_series_groups_by_label_set_and_time_sorts():
    from knausen_signal.db import StoredSample
    r1 = StoredSample(id=1, ts=1000.0, payload={"rsrp_dbm": -70, "snr_db": 10,
                                                "connected": True})
    r2 = StoredSample(id=2, ts=2000.0, payload={"rsrp_dbm": -71, "snr_db": 11,
                                                "connected": True})
    series = build_series([r2, r1], [])  # out-of-order on purpose

    rsrp = _series_named(series, "knausen_modem_rsrp_dbm")
    assert [s.value for s in rsrp.samples] == [-70.0, -71.0]
    assert [s.timestamp_ms for s in rsrp.samples] == [1_000_000, 2_000_000]


def test_build_series_returns_empty_for_no_rows():
    assert build_series([], []) == []


# ---------- push_unpushed end-to-end ----------

def test_push_unpushed_drains_marks_and_pushes(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = open_db(db_path)

    insert_modem_sample(conn, ts=1000.0, payload=MODEM_PAYLOAD)
    insert_probe_sample(conn, ts=1001.0, payload=PROBE_PAYLOAD)

    cfg = _fake_config()
    captured = {}

    def fake_push(url, user, password, series, **_):
        captured["called"] = True
        captured["series_count"] = len(series)

    with patch("knausen_signal.push.push", side_effect=fake_push):
        pushed = push_unpushed(conn, cfg)

    assert pushed == 2
    assert captured["called"]
    assert captured["series_count"] > 5  # many metrics emitted

    # Both rows marked pushed
    unp_modem = conn.execute(
        "SELECT count(*) FROM modem_sample WHERE pushed_at IS NULL"
    ).fetchone()[0]
    unp_probe = conn.execute(
        "SELECT count(*) FROM probe_sample WHERE pushed_at IS NULL"
    ).fetchone()[0]
    assert unp_modem == 0
    assert unp_probe == 0


def test_push_unpushed_returns_zero_when_nothing_pending(tmp_path):
    conn = open_db(tmp_path / "test.sqlite")
    pushed = push_unpushed(conn, _fake_config())
    assert pushed == 0


def test_push_unpushed_drains_mtr_snapshots_alongside_other_tables(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = open_db(db_path)

    insert_mtr_snapshot(conn, ts=1500.0, payload=MTR_PAYLOAD)

    captured = {}

    def fake_push(url, user, password, series, **_):
        captured["series_count"] = len(series)
        captured["names"] = {
            next(l.value for l in s.labels if l.name == "__name__")
            for s in series
        }

    with patch("knausen_signal.push.push", side_effect=fake_push):
        pushed = push_unpushed(conn, _fake_config())

    assert pushed == 1
    # 5 data metrics across 2 hops would collapse onto 5 series (each
    # series has 2 samples? — no, different hop_num labels = 2 series per
    # metric). So 5 data * 2 hops + 1 info * 2 hops = 12 series total.
    assert captured["series_count"] == 12
    assert "knausen_mtr_hop_rtt_ms_worst" in captured["names"]
    assert "knausen_mtr_hop_info" in captured["names"]

    unp_mtr = conn.execute(
        "SELECT count(*) FROM mtr_snapshot WHERE pushed_at IS NULL"
    ).fetchone()[0]
    assert unp_mtr == 0


def test_push_unpushed_leaves_rows_unpushed_on_remote_error(tmp_path):
    db_path = tmp_path / "test.sqlite"
    conn = open_db(db_path)
    insert_modem_sample(conn, ts=time.time(), payload=MODEM_PAYLOAD)

    with patch("knausen_signal.push.push", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            push_unpushed(conn, _fake_config())

    still_unpushed = conn.execute(
        "SELECT count(*) FROM modem_sample WHERE pushed_at IS NULL"
    ).fetchone()[0]
    assert still_unpushed == 1


# ---------- helpers ----------

def _find(payload, metric_name):
    for n, _, v in _modem_metrics(payload):
        if n == metric_name:
            return v
    return None


def _series_named(series, name):
    for s in series:
        for lab in s.labels:
            if lab.name == "__name__" and lab.value == name:
                return s
    raise AssertionError(f"series {name!r} not in {[[l.value for l in s.labels] for s in series]}")


def _fake_config():
    from knausen_signal.config import (
        Config,
        ModemConfig,
        MtrConfig,
        ProbeConfig,
        PushConfig,
    )
    return Config(
        db_path=":memory:",
        log_level="INFO",
        modem=ModemConfig(host="h", ssh_key_path="/dev/null", ssh_user="admin", interval_sec=900),
        probe=ProbeConfig(
            interval_sec=900,
            ping_targets=["1.1.1.1"],
            checkpoints=[("gateway", "192.168.1.1")],
        ),
        mtr=MtrConfig(
            enabled=True, target="8.8.8.8", trigger_p95_ms=500.0,
            cooldown_sec=600, probe_count=30,
        ),
        push=PushConfig(
            interval_sec=60,
            prometheus_url="https://example.com/api/prom/push",
            prometheus_user="user",
            prometheus_password="pass",
        ),
    )
