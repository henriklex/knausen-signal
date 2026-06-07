"""Supervisor: asyncio loop scheduling modem poll / probe / push.

Three independent coroutines, each with its own retry/backoff state:

    modem_loop  — every N seconds: client.poll() → insert_modem_sample
    probe_loop  — every N seconds: run_probe()   → insert_probe_sample
    push_loop   — every N seconds: drain unpushed rows + heartbeat

Backoff:
- modem lockout (errno 6): sleep the indicated seconds + 10s slack
- modem other failure: 60s
- probe failure: ignored (logged); next cycle on the regular interval
- push failure: exponential 2^n * interval_sec, capped at 1h

Heartbeat: every push cycle pushes a single `knausen_collector_heartbeat=1`
sample so an absence of recent heartbeat in Grafana Cloud means the
collector itself died — distinguishable from "the internet is broken but
the collector is still running and queueing".
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
import time

from .config import Config
from .db import insert_modem_sample, insert_probe_sample, open_db
from .modem import ZyxelLockoutError, ZyxelLTE7460Client
from .probe import run_probe
from .push import push_unpushed
from .remote_write import Label, Sample, TimeSeries, push as remote_push

log = logging.getLogger(__name__)


async def modem_loop(cfg: Config, conn: sqlite3.Connection) -> None:
    client = ZyxelLTE7460Client(cfg.modem.host, cfg.modem.username, cfg.modem.password)
    while True:
        backoff = 0
        try:
            sample = await asyncio.to_thread(client.poll)
            await asyncio.to_thread(
                insert_modem_sample, conn, time.time(), sample.as_dict()
            )
            log.info(
                "modem: rsrp=%s rsrq=%s snr=%s rssi=%s",
                sample.rsrp_dbm, sample.rsrq_db, sample.snr_db, sample.rssi_dbm,
            )
        except ZyxelLockoutError as e:
            backoff = e.seconds_remaining + 10
            log.warning("modem: lockout, sleeping %d s", backoff)
        except Exception:
            backoff = 60
            log.exception("modem: poll failed, sleeping %d s", backoff)
        await asyncio.sleep(backoff or cfg.modem.interval_sec)


async def probe_loop(cfg: Config, conn: sqlite3.Connection) -> None:
    while True:
        try:
            sample = await asyncio.to_thread(
                run_probe, ping_targets=tuple(cfg.probe.ping_targets)
            )
            await asyncio.to_thread(
                insert_probe_sample, conn, time.time(), sample.as_dict()
            )
            log.info(
                "probe: ok=%s ping_p50=%s loss=%s",
                sample.probe_ok, sample.ping_rtt_ms_p50, sample.ping_loss_pct,
            )
        except Exception:
            log.exception("probe: run failed")
        await asyncio.sleep(cfg.probe.interval_sec)


async def push_loop(cfg: Config, conn: sqlite3.Connection) -> None:
    consecutive_failures = 0
    while True:
        try:
            count = await asyncio.to_thread(push_unpushed, conn, cfg)
            heartbeat = [
                TimeSeries(
                    labels=(Label("__name__", "knausen_collector_heartbeat"),),
                    samples=(Sample(1.0, int(time.time() * 1000)),),
                )
            ]
            await asyncio.to_thread(
                remote_push,
                cfg.push.prometheus_url,
                cfg.push.prometheus_user,
                cfg.push.prometheus_password,
                heartbeat,
            )
            consecutive_failures = 0
            if count:
                log.info("push: drained %d rows", count)
        except Exception:
            consecutive_failures += 1
            backoff = min(
                cfg.push.interval_sec * (2 ** min(consecutive_failures, 6)), 3600
            )
            log.exception(
                "push: failed (attempt %d), backing off %d s",
                consecutive_failures, backoff,
            )
            await asyncio.sleep(backoff)
            continue
        await asyncio.sleep(cfg.push.interval_sec)


async def supervisor(cfg: Config, conn: sqlite3.Connection) -> None:
    """Run the three loops until SIGTERM/SIGINT or any loop crashes."""
    tasks = [
        asyncio.create_task(modem_loop(cfg, conn), name="modem"),
        asyncio.create_task(probe_loop(cfg, conn), name="probe"),
        asyncio.create_task(push_loop(cfg, conn), name="push"),
    ]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # signal handlers don't work on Windows / inside some test runners;
            # tasks can still be cancelled directly.
            pass
    stop_task = asyncio.create_task(stop.wait(), name="stop")

    try:
        await asyncio.wait(
            [*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stop_task.cancel()


def main() -> int:
    cfg = Config.from_env()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info(
        "knausen-signal starting: db=%s modem=%ss probe=%ss push=%ss",
        cfg.db_path, cfg.modem.interval_sec, cfg.probe.interval_sec,
        cfg.push.interval_sec,
    )
    conn = open_db(cfg.db_path)
    try:
        asyncio.run(supervisor(cfg, conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
