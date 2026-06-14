"""Tests for the triggered mtr snapshot runner.

The subprocess itself is mocked; the JSON parser is exercised against a
real mtr fixture so the schema we depend on (`report.hubs` with
string-typed numeric fields) is locked down.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from knausen_signal import mtr
from knausen_signal.mtr import MtrHop, MtrSnapshot, _parse_hubs, run_mtr


# ---------- _parse_hubs against a real fixture ----------

def test_parse_hubs_against_real_mtr_fixture(fixture):
    data = fixture("mtr_8888.json")
    hops = _parse_hubs(data)
    assert hops is not None
    assert len(hops) == 5

    by_num = {h.hop_num: h for h in hops}
    assert by_num[1].host == "192.168.1.1"
    assert by_num[1].rtt_best == pytest.approx(1.4)

    # The "???" hops (carrier core, ICMP TTL-exceeded suppressed) parse
    # cleanly with 100% loss — they're not errors, they're policy.
    assert by_num[2].host == "???"
    assert by_num[2].loss_pct == pytest.approx(100.0)

    # Carrier-edge hop — the diagnostic line.
    assert by_num[4].host == "10.4.208.17"
    assert by_num[4].rtt_worst == pytest.approx(197.2)
    assert by_num[4].rtt_stdev == pytest.approx(39.5)


def test_parse_hubs_missing_report_returns_none():
    assert _parse_hubs({}) is None
    assert _parse_hubs({"report": {}}) is None


def test_parse_hubs_skips_malformed_hop_keeps_others():
    data = {"report": {"hubs": [
        {"count": 1, "host": "a", "Loss%": 0.0, "Snt": 1, "Last": 1.0,
         "Avg": 1.0, "Best": 1.0, "Wrst": 1.0, "StDev": 0.0},
        {"count": "not-an-int", "host": "b"},  # malformed
        {"count": 3, "host": "c", "Loss%": 0.0, "Snt": 1, "Last": 1.0,
         "Avg": 1.0, "Best": 1.0, "Wrst": 1.0, "StDev": 0.0},
    ]}}
    hops = _parse_hubs(data)
    assert hops is not None
    assert [h.host for h in hops] == ["a", "c"]


# ---------- run_mtr subprocess behaviors ----------

def _completed(stdout: str = "{}", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_run_mtr_returns_snapshot_on_success(fixture):
    fake_stdout = json.dumps(fixture("mtr_8888.json"))
    with patch.object(mtr, "shutil") as fake_shutil, \
         patch.object(mtr.subprocess, "run", return_value=_completed(stdout=fake_stdout)):
        fake_shutil.which.return_value = "/usr/bin/mtr"
        snap = run_mtr("8.8.8.8", count=30)

    assert isinstance(snap, MtrSnapshot)
    assert snap.target == "8.8.8.8"
    assert len(snap.hops) == 5
    assert isinstance(snap.hops[0], MtrHop)


def test_run_mtr_returns_none_when_binary_missing():
    with patch.object(mtr, "shutil") as fake_shutil:
        fake_shutil.which.return_value = None
        assert run_mtr("8.8.8.8") is None


def test_run_mtr_returns_none_on_timeout():
    with patch.object(mtr, "shutil") as fake_shutil, \
         patch.object(mtr.subprocess, "run",
                      side_effect=subprocess.TimeoutExpired(cmd="mtr", timeout=10)):
        fake_shutil.which.return_value = "/usr/bin/mtr"
        assert run_mtr("8.8.8.8") is None


def test_run_mtr_returns_none_on_nonzero_exit():
    with patch.object(mtr, "shutil") as fake_shutil, \
         patch.object(mtr.subprocess, "run",
                      return_value=_completed(returncode=2, stderr="boom")):
        fake_shutil.which.return_value = "/usr/bin/mtr"
        assert run_mtr("8.8.8.8") is None


def test_run_mtr_returns_none_on_malformed_json():
    with patch.object(mtr, "shutil") as fake_shutil, \
         patch.object(mtr.subprocess, "run",
                      return_value=_completed(stdout="not json")):
        fake_shutil.which.return_value = "/usr/bin/mtr"
        assert run_mtr("8.8.8.8") is None
