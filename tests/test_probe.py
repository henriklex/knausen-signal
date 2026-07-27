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
         patch.object(probe, "_time_tcp", return_value=20.0), \
         patch.object(probe, "_time_tls", return_value=45.0), \
         patch.object(probe, "_time_https_head", return_value=80.0):
        mp.side_effect = [
            _fake_ping_host([10, 11, 12, 13, 14, 15, 16, 17, 18, 19], 0.0),
            _fake_ping_host([20, 21, 22, 23, 24, 25, 26, 27, 28, 29], 0.0),
            _fake_ping_host([30, 31, 32, 33, 34, 35, 36, 37, 38, 39], 0.1),
        ]
        # no dns_resolvers -> DNS not probed, doesn't affect probe_ok
        sample = run_probe(ping_targets=("a", "b", "c"), checkpoints=())

    assert isinstance(sample, ProbeSample)
    assert sample.probe_ok is True
    assert sample.ping_loss_pct == pytest.approx(10.0)  # worst single-target
    # p50 of 30 values 10..39 -> 24.5
    assert sample.ping_rtt_ms_p50 == pytest.approx(24.5)
    assert sample.dns == ()
    assert sample.tcp_connect_ms == 20.0
    assert sample.tls_handshake_ms == 45.0
    assert sample.https_head_ms == 80.0


# ---------- failure isolation ----------

def test_run_probe_dns_total_failure_flips_probe_ok():
    """Every configured resolver failing (total DNS outage) flips probe_ok;
    other sub-probes stay isolated and intact."""
    failed = [probe.DnsResult("1.1.1.1", "v4", "handed", None, 100.0)]
    with patch.object(probe, "icmp_ping") as mp, \
         patch.object(probe, "_probe_dns_resolvers", return_value=failed), \
         patch.object(probe, "_time_tcp", return_value=20.0), \
         patch.object(probe, "_time_tls", return_value=45.0), \
         patch.object(probe, "_time_https_head", return_value=80.0):
        mp.return_value = _fake_ping_host([10, 11, 12], 0.0)
        sample = run_probe(
            ping_targets=("a",), checkpoints=(),
            dns_resolvers=[("1.1.1.1", "v4", "handed")],
        )

    assert sample.tcp_connect_ms == 20.0
    assert sample.dns[0].rtt_ms_p50 is None
    assert sample.probe_ok is False


def test_run_probe_one_dead_resolver_does_not_flip_ok_if_another_works():
    """A single dead resolver is a per-line signal, not a probe failure."""
    mixed = [
        probe.DnsResult("1.1.1.1", "v4", "handed", 42.0, 0.0),
        probe.DnsResult("2a01:798:0:8012::4", "v6", "handed", None, 100.0),
    ]
    with patch.object(probe, "icmp_ping") as mp, \
         patch.object(probe, "_probe_dns_resolvers", return_value=mixed), \
         patch.object(probe, "_time_tcp", return_value=20.0), \
         patch.object(probe, "_time_tls", return_value=45.0), \
         patch.object(probe, "_time_https_head", return_value=80.0):
        mp.return_value = _fake_ping_host([10, 11, 12], 0.0)
        sample = run_probe(
            ping_targets=("a",), checkpoints=(),
            dns_resolvers=[("1.1.1.1", "v4", "handed")],
        )

    assert sample.probe_ok is True
    assert len(sample.dns) == 2


def test_run_probe_all_ping_targets_down_reports_loss_100_but_probe_ok_stays_true():
    """100% packet loss is a network condition, not a probe error.
    probe_ok stays True; the loss_pct=100 is the signal."""
    with patch.object(probe, "icmp_ping") as mp, \
         patch.object(probe, "_time_tcp", return_value=20.0), \
         patch.object(probe, "_time_tls", return_value=45.0), \
         patch.object(probe, "_time_https_head", return_value=80.0):
        mp.side_effect = [
            _fake_ping_host([], 1.0),
            _fake_ping_host([], 1.0),
            _fake_ping_host([], 1.0),
        ]
        sample = run_probe(ping_targets=("a", "b", "c"), checkpoints=())

    assert sample.ping_loss_pct == pytest.approx(100.0)
    assert sample.ping_rtt_ms_p50 is None
    assert sample.ping_rtt_ms_p95 is None
    assert sample.probe_ok is True


def test_run_probe_partial_ping_loss_yields_percentiles_and_worst_case_loss():
    """Only target B failed; A and C still give us RTT data."""
    with patch.object(probe, "icmp_ping") as mp, \
         patch.object(probe, "_time_tcp", return_value=20.0), \
         patch.object(probe, "_time_tls", return_value=45.0), \
         patch.object(probe, "_time_https_head", return_value=80.0):
        mp.side_effect = [
            _fake_ping_host([10, 11, 12], 0.0),
            _fake_ping_host([], 1.0),
            _fake_ping_host([14, 15, 16], 0.0),
        ]
        sample = run_probe(ping_targets=("a", "b", "c"), checkpoints=())

    assert sample.ping_loss_pct == pytest.approx(100.0)  # worst-case across targets
    assert sample.ping_rtt_ms_p50 is not None
    assert sample.probe_ok is True


def test_run_probe_ping_subprobe_raises_sets_probe_ok_false():
    """If the ping code itself blows up (vs. just losing packets), probe_ok flips."""
    with patch.object(probe, "icmp_ping", side_effect=OSError("ICMP not permitted")), \
         patch.object(probe, "_time_tcp", return_value=20.0), \
         patch.object(probe, "_time_tls", return_value=45.0), \
         patch.object(probe, "_time_https_head", return_value=80.0):
        sample = run_probe(ping_targets=("a",), checkpoints=())

    assert sample.ping_rtt_ms_p50 is None
    assert sample.ping_loss_pct is None
    assert sample.probe_ok is False


# ---------- checkpoint pings ----------

def test_ping_per_checkpoint_populates_dicts_keyed_by_name():
    """Each checkpoint gets one icmp_ping call; results land under its name."""
    with patch.object(probe, "icmp_ping") as mp:
        mp.side_effect = [
            _fake_ping_host([1.0, 2.0, 3.0], 0.0),    # gateway
            _fake_ping_host([40.0, 45.0, 50.0], 0.0), # carrier_edge
        ]
        p50, p95, loss = probe._ping_per_checkpoint(
            [("gateway", "192.168.1.1"), ("carrier_edge", "10.4.208.17")],
            count=3,
            privileged=False,
        )

    assert set(p50.keys()) == {"gateway", "carrier_edge"}
    assert p50["gateway"] == pytest.approx(2.0)
    assert p50["carrier_edge"] == pytest.approx(45.0)
    assert loss["gateway"] == pytest.approx(0.0)
    assert loss["carrier_edge"] == pytest.approx(0.0)


def test_ping_per_checkpoint_one_failure_isolated_to_its_key():
    """An unreachable checkpoint produces None for its name; others survive."""
    with patch.object(probe, "icmp_ping") as mp:
        mp.side_effect = [
            _fake_ping_host([1.0, 2.0, 3.0], 0.0),  # gateway ok
            OSError("no route to host"),            # carrier_edge dead
        ]
        p50, p95, loss = probe._ping_per_checkpoint(
            [("gateway", "192.168.1.1"), ("carrier_edge", "10.4.208.17")],
            count=3,
            privileged=False,
        )

    assert p50["gateway"] == pytest.approx(2.0)
    assert p50["carrier_edge"] is None
    assert p95["carrier_edge"] is None
    assert loss["carrier_edge"] is None


def test_run_probe_populates_checkpoint_fields():
    """run_probe with checkpoints fills the three dict fields on the sample."""
    with patch.object(probe, "icmp_ping") as mp, \
         patch.object(probe, "_time_tcp", return_value=20.0), \
         patch.object(probe, "_time_tls", return_value=45.0), \
         patch.object(probe, "_time_https_head", return_value=80.0):
        mp.side_effect = [
            _fake_ping_host([10, 11, 12], 0.0),  # aggregate target "a"
            _fake_ping_host([1, 2, 3], 0.0),     # checkpoint gateway
            _fake_ping_host([40, 45, 50], 0.0),  # checkpoint carrier_edge
        ]
        sample = run_probe(
            ping_targets=("a",),
            checkpoints=(("gateway", "192.168.1.1"),
                         ("carrier_edge", "10.4.208.17")),
            checkpoint_count=3,
        )

    assert sample.checkpoint_rtt_ms_p50 == {
        "gateway": pytest.approx(2.0),
        "carrier_edge": pytest.approx(45.0),
    }
    assert sample.checkpoint_loss_pct["gateway"] == pytest.approx(0.0)
    assert sample.probe_ok is True


def test_run_probe_empty_checkpoints_leaves_dicts_empty():
    """checkpoints=() means no checkpoint pings, empty dicts."""
    with patch.object(probe, "icmp_ping") as mp, \
         patch.object(probe, "_time_tcp", return_value=20.0), \
         patch.object(probe, "_time_tls", return_value=45.0), \
         patch.object(probe, "_time_https_head", return_value=80.0):
        mp.return_value = _fake_ping_host([10, 11, 12], 0.0)
        sample = run_probe(ping_targets=("a",), checkpoints=())

    assert sample.checkpoint_rtt_ms_p50 == {}
    assert sample.checkpoint_rtt_ms_p95 == {}
    assert sample.checkpoint_loss_pct == {}


# ---------- DNS: resolver auto-detect ----------

def test_detect_handed_resolvers_parses_v4_and_v6_terse_output():
    # nmcli terse mode escapes ':' as '\:' in IPv6 values
    terse = (
        "IP4.DNS[1]:1.1.1.1\n"
        "IP4.DNS[2]:8.8.8.8\n"
        "IP6.DNS[1]:2a01\\:798\\:0\\:8012\\:\\:4\n"
    )
    with patch.object(probe.subprocess, "run",
                      return_value=SimpleNamespace(stdout=terse)):
        out = probe.detect_handed_resolvers("eth0")
    assert ("1.1.1.1", "v4", "handed") in out
    assert ("8.8.8.8", "v4", "handed") in out
    assert ("2a01:798:0:8012::4", "v6", "handed") in out


def test_detect_handed_resolvers_returns_empty_on_error():
    with patch.object(probe.subprocess, "run",
                      side_effect=FileNotFoundError("nmcli")):
        assert probe.detect_handed_resolvers() == []


def test_assemble_resolvers_dedups_and_labels():
    handed = [("1.1.1.1", "v4", "handed"),
              ("2a01:798:0:8012::4", "v6", "handed")]
    with patch.object(probe, "detect_handed_resolvers", return_value=handed):
        out = probe.assemble_resolvers(
            autodetect=True, interface="eth0",
            reference_servers=["1.1.1.1", "2606:4700:4700::1111"],
        )
    by_ip = {ip: (fam, src) for ip, fam, src in out}
    # 1.1.1.1 handed wins, not duplicated as reference
    assert [ip for ip, _, _ in out].count("1.1.1.1") == 1
    assert by_ip["1.1.1.1"] == ("v4", "handed")
    # reference-only anchor added with inferred family
    assert by_ip["2606:4700:4700::1111"] == ("v6", "reference")


def test_assemble_resolvers_autodetect_off_uses_only_reference():
    with patch.object(probe, "detect_handed_resolvers") as d:
        out = probe.assemble_resolvers(
            autodetect=False, interface="eth0", reference_servers=["1.1.1.1"],
        )
    d.assert_not_called()
    assert out == [("1.1.1.1", "v4", "reference")]


# ---------- DNS: per-resolver measurement ----------

def test_probe_dns_resolvers_success_and_failure_isolated():
    class _OK:
        def __init__(self, *a, **k):
            self.nameservers, self.lifetime = [], 0
        def resolve(self, domain, rtype):
            return None

    class _Fail:
        def __init__(self, *a, **k):
            self.nameservers, self.lifetime = [], 0
        def resolve(self, domain, rtype):
            raise OSError("SERVFAIL")

    with patch.object(probe.dns.resolver, "Resolver", _OK):
        ok = probe._probe_dns_resolvers(
            [("1.1.1.1", "v4", "handed")], ["a.com", "b.com"])
    assert ok[0].resolver == "1.1.1.1"
    assert ok[0].loss_pct == pytest.approx(0.0)
    assert ok[0].rtt_ms_p50 is not None

    with patch.object(probe.dns.resolver, "Resolver", _Fail):
        bad = probe._probe_dns_resolvers(
            [("9.9.9.9", "v4", "reference")], ["a.com", "b.com"])
    assert bad[0].rtt_ms_p50 is None
    assert bad[0].loss_pct == pytest.approx(100.0)
