"""Supervisor smoke tests.

Each test patches the heavy sub-components (network/router/HTTP) to fast
no-ops, runs the supervisor for a short window, and asserts the loops did
at least one cycle and shut down cleanly.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from knausen_signal import main as main_mod
from knausen_signal.config import (
    Config, ModemConfig, ProbeConfig, PushConfig,
)
from knausen_signal.db import open_db
from knausen_signal.modem import ModemSample
from knausen_signal.probe import ProbeSample


def _config(tmp_path):
    return Config(
        db_path=str(tmp_path / "data.sqlite"),
        log_level="INFO",
        modem=ModemConfig(host="r", username="u", password="p", interval_sec=999),
        probe=ProbeConfig(interval_sec=999, ping_targets=["1.1.1.1"]),
        push=PushConfig(
            interval_sec=999,
            prometheus_url="https://example.com/api/prom/push",
            prometheus_user="user",
            prometheus_password="pass",
        ),
    )


def _modem_sample():
    return ModemSample(
        connected=True, rsrp_dbm=-61, rsrq_db=-9, snr_db=25, rssi_dbm=-31,
        cqi=-1, operator="ice+", mcc=242, mnc=14, network_type="LTE",
        band_primary="E_UTRA_3", earfcn_primary=1850,
        band_secondary=None, earfcn_secondary=None,
        tac=20001, cid=15129611, pci=250,
        ipv4_address="100.1.1.1", ipv4_connection_time="00:00:00:01",
        ipv6_address=None,
    )


def _probe_sample():
    return ProbeSample(
        ping_rtt_ms_p50=50.0, ping_rtt_ms_p95=100.0, ping_loss_pct=0.0,
        dns_lookup_ms=10.0, tcp_connect_ms=20.0, tls_handshake_ms=30.0,
        https_head_ms=40.0, probe_ok=True,
    )


async def test_supervisor_runs_one_cycle_of_each_loop(tmp_path):
    cfg = _config(tmp_path)
    conn = open_db(cfg.db_path)

    with patch.object(main_mod, "ZyxelLTE7460Client") as MockClient, \
         patch.object(main_mod, "run_probe", return_value=_probe_sample()), \
         patch.object(main_mod, "remote_push") as mock_remote_push, \
         patch.object(main_mod, "push_unpushed", return_value=2) as mock_push:
        MockClient.return_value.poll.return_value = _modem_sample()

        task = asyncio.create_task(main_mod.supervisor(cfg, conn))
        await asyncio.sleep(0.2)  # let each loop run once
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Each loop wrote a row + push was invoked at least once
    modem_count = conn.execute("SELECT count(*) FROM modem_sample").fetchone()[0]
    probe_count = conn.execute("SELECT count(*) FROM probe_sample").fetchone()[0]
    assert modem_count >= 1
    assert probe_count >= 1
    assert mock_push.called
    assert mock_remote_push.called  # heartbeat pushed


async def test_supervisor_modem_lockout_does_not_crash(tmp_path):
    """A lockout should be caught and the loop should keep running."""
    from knausen_signal.modem import ZyxelLockoutError
    cfg = _config(tmp_path)
    conn = open_db(cfg.db_path)

    with patch.object(main_mod, "ZyxelLTE7460Client") as MockClient, \
         patch.object(main_mod, "run_probe", return_value=_probe_sample()), \
         patch.object(main_mod, "remote_push"), \
         patch.object(main_mod, "push_unpushed", return_value=0):
        MockClient.return_value.poll.side_effect = ZyxelLockoutError(1)

        task = asyncio.create_task(main_mod.supervisor(cfg, conn))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Modem loop didn't insert (it locked out) but the supervisor stayed up,
    # so probe rows are present.
    probe_count = conn.execute("SELECT count(*) FROM probe_sample").fetchone()[0]
    assert probe_count >= 1
