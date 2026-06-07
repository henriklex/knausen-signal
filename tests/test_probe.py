"""Tests for the internet-quality probe.

All network is mocked. The point of these tests is to verify shape, the
percentile helper, the per-sub-probe error isolation, and the probe_ok
aggregation rule.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from knausen_signal import probe
from knausen_signal.probe import ProbeSample, percentile, run_probe


# ---------- percentile ----------

def test_percentile_p50_odd_count():
    assert percentile([1.0, 2.0, 3.0], 50) == 2.0


def test_percentile_p50_even_count_interpolates():
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5


def test_percentile_p95_small_sample():
    # 10 items: p95 falls between index 8 and 9, 95% of the way to 9
    vals = [1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(vals, 95) == pytest.approx(9.55, abs=0.01)


def test_percentile_empty_raises():
    with pytest.raises(ValueError):
        percentile([], 50)


# ---------- run_probe shape, all-ok path ----------

def _fake_ping_host(rtts, loss):
    return SimpleNamespace(rtts=list(rtts), packet_loss=loss)


def test_run_probe_all_ok_aggregates_pings_and_sets_ok_true():
    with patch.object(probe, "icmp_ping") as mp, \
         patch.object(probe, "_time_dns", return_value=12.3), \
         patch.object(probe, "_time_tcp", return_value=20.0), \
         patch.object(probe, "_time_tls", return_value=45.0), \
         patch.object(probe, "_time_https_head", return_value=80.0):
        mp.side_effect = [
            _fake_ping_host([10, 11, 12, 13, 14, 15, 16, 17, 18, 19], 0.0),
            _fake_ping_host([20, 21, 22, 23, 24, 25, 26, 27, 28, 29], 0.0),
            _fake_ping_host([30, 31, 32, 33, 34, 35, 36, 37, 38, 39], 0.1),
        ]
        sample = run_probe(ping_targets=("a", "b", "c"))

    assert isinstance(sample, ProbeSample)
    assert sample.probe_ok is True
    assert sample.ping_loss_pct == pytest.approx(10.0)  # worst single-target
    # p50 of 30 values 10..39 -> 24.5
    assert sample.ping_rtt_ms_p50 == pytest.approx(24.5)
    assert sample.dns_lookup_ms == 12.3
    assert sample.tcp_connect_ms == 20.0
    assert sample.tls_handshake_ms == 45.0
    assert sample.https_head_ms == 80.0


# ---------- failure isolation ----------

def test_run_probe_dns_failure_does_not_kill_other_probes():
    with patch.object(probe, "icmp_ping") as mp, \
         patch.object(probe, "_time_dns", side_effect=RuntimeError("dns down")), \
         patch.object(probe, "_time_tcp", return_value=20.0), \
         patch.object(probe, "_time_tls", return_value=45.0), \
         patch.object(probe, "_time_https_head", return_value=80.0):
        mp.return_value = _fake_ping_host([10, 11, 12], 0.0)
        sample = run_probe(ping_targets=("a",))

    assert sample.dns_lookup_ms is None
    assert sample.tcp_connect_ms == 20.0
    assert sample.probe_ok is False


def test_run_probe_all_ping_targets_down_reports_loss_100_but_probe_ok_stays_true():
    """100% packet loss is a network condition, not a probe error.
    probe_ok stays True; the loss_pct=100 is the signal."""
    with patch.object(probe, "icmp_ping") as mp, \
         patch.object(probe, "_time_dns", return_value=12.0), \
         patch.object(probe, "_time_tcp", return_value=20.0), \
         patch.object(probe, "_time_tls", return_value=45.0), \
         patch.object(probe, "_time_https_head", return_value=80.0):
        mp.side_effect = [
            _fake_ping_host([], 1.0),
            _fake_ping_host([], 1.0),
            _fake_ping_host([], 1.0),
        ]
        sample = run_probe(ping_targets=("a", "b", "c"))

    assert sample.ping_loss_pct == pytest.approx(100.0)
    assert sample.ping_rtt_ms_p50 is None
    assert sample.ping_rtt_ms_p95 is None
    assert sample.probe_ok is True


def test_run_probe_partial_ping_loss_yields_percentiles_and_worst_case_loss():
    """Only target B failed; A and C still give us RTT data."""
    with patch.object(probe, "icmp_ping") as mp, \
         patch.object(probe, "_time_dns", return_value=12.0), \
         patch.object(probe, "_time_tcp", return_value=20.0), \
         patch.object(probe, "_time_tls", return_value=45.0), \
         patch.object(probe, "_time_https_head", return_value=80.0):
        mp.side_effect = [
            _fake_ping_host([10, 11, 12], 0.0),
            _fake_ping_host([], 1.0),
            _fake_ping_host([14, 15, 16], 0.0),
        ]
        sample = run_probe(ping_targets=("a", "b", "c"))

    assert sample.ping_loss_pct == pytest.approx(100.0)  # worst-case across targets
    assert sample.ping_rtt_ms_p50 is not None
    assert sample.probe_ok is True


def test_run_probe_ping_subprobe_raises_sets_probe_ok_false():
    """If the ping code itself blows up (vs. just losing packets), probe_ok flips."""
    with patch.object(probe, "icmp_ping", side_effect=OSError("ICMP not permitted")), \
         patch.object(probe, "_time_dns", return_value=12.0), \
         patch.object(probe, "_time_tcp", return_value=20.0), \
         patch.object(probe, "_time_tls", return_value=45.0), \
         patch.object(probe, "_time_https_head", return_value=80.0):
        sample = run_probe(ping_targets=("a",))

    assert sample.ping_rtt_ms_p50 is None
    assert sample.ping_loss_pct is None
    assert sample.probe_ok is False
