"""Push worker — drain unpushed SQLite rows to Grafana Cloud via remote_write.

Each call to `push_unpushed(conn, cfg)` is one drain cycle:
    1. Select up to BATCH_SIZE rows from each of `modem_sample` and
       `probe_sample` where pushed_at IS NULL.
    2. Project each row into one or more Prometheus time series.
    3. POST the snappy-block protobuf payload.
    4. On HTTP 2xx, mark every selected row as pushed (UPDATE pushed_at).
    5. On any error, leave rows unpushed — next cycle retries.

This means: when the WAN is down (the thing we most want to see), samples
accumulate locally and replay in order on reconnect. The local SQLite is
also the long-term archive — it is never auto-pruned, so post-mortems
beyond Grafana Cloud's 14-day retention still work by reading the file.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterable
from typing import Any

from .config import Config
from .db import StoredSample, mark_pushed, select_unpushed
from .remote_write import Label, Sample, TimeSeries, push

log = logging.getLogger(__name__)

BATCH_SIZE = 500
METRIC_PREFIX = "knausen"

# ---------- modem metric projection ----------

# (metric suffix, payload key) for the plain numeric gauges.
_MODEM_NUMERIC: tuple[tuple[str, str], ...] = (
    ("modem_rsrp_dbm",         "rsrp_dbm"),
    ("modem_rsrq_db",          "rsrq_db"),
    ("modem_snr_db",           "snr_db"),
    ("modem_rssi_dbm",         "rssi_dbm"),
    ("modem_cqi",              "cqi"),
    ("modem_pci",              "pci"),
    ("modem_cid",              "cid"),
    ("modem_tac",              "tac"),
    ("modem_mcc",              "mcc"),
    ("modem_mnc",              "mnc"),
    ("modem_earfcn_primary",   "earfcn_primary"),
    ("modem_earfcn_secondary", "earfcn_secondary"),
)


def _modem_metrics(payload: dict[str, Any]) -> Iterable[tuple[str, tuple[Label, ...], float]]:
    """Yield (metric_name, labels_extra, value) for one modem sample.

    Numeric gauges with `None` values are skipped — Prometheus handles gaps
    fine and emitting NaN just clutters the dashboard.
    """
    for suffix, key in _MODEM_NUMERIC:
        v = payload.get(key)
        if v is not None:
            yield f"{METRIC_PREFIX}_{suffix}", (), float(v)

    # Booleans → 0/1 floats
    connected = payload.get("connected")
    if connected is not None:
        yield f"{METRIC_PREFIX}_modem_connected", (), 1.0 if connected else 0.0

    # Derived: 1 when carrier aggregation is active, 0 otherwise.
    ca = payload.get("band_secondary") is not None
    yield f"{METRIC_PREFIX}_modem_carrier_aggregation", (), 1.0 if ca else 0.0

    # info series — low-cardinality string context as a constant=1 gauge.
    info_labels = tuple(
        Label(k, str(payload.get(k) or ""))
        for k in ("operator", "network_type", "band_primary", "band_secondary")
    )
    yield f"{METRIC_PREFIX}_modem_info", info_labels, 1.0


# ---------- probe metric projection ----------

_PROBE_NUMERIC: tuple[tuple[str, str], ...] = (
    ("probe_ping_rtt_ms_p50",   "ping_rtt_ms_p50"),
    ("probe_ping_rtt_ms_p95",   "ping_rtt_ms_p95"),
    ("probe_ping_loss_pct",     "ping_loss_pct"),
    ("probe_dns_lookup_ms",     "dns_lookup_ms"),
    ("probe_tcp_connect_ms",    "tcp_connect_ms"),
    ("probe_tls_handshake_ms",  "tls_handshake_ms"),
    ("probe_https_head_ms",     "https_head_ms"),
)


def _probe_metrics(payload: dict[str, Any]) -> Iterable[tuple[str, tuple[Label, ...], float]]:
    for suffix, key in _PROBE_NUMERIC:
        v = payload.get(key)
        if v is not None:
            yield f"{METRIC_PREFIX}_{suffix}", (), float(v)
    ok = payload.get("probe_ok")
    if ok is not None:
        yield f"{METRIC_PREFIX}_probe_ok", (), 1.0 if ok else 0.0


# ---------- series assembly ----------

def build_series(
    modem_rows: list[StoredSample],
    probe_rows: list[StoredSample],
) -> list[TimeSeries]:
    """Group all yielded points by label set, sort samples by ts.

    Prometheus remote_write wants samples within one TimeSeries to be
    time-sorted and unique-per-timestamp, so we group + sort here.
    """
    bucket: dict[tuple[Label, ...], list[Sample]] = {}

    def emit(name: str, extra: tuple[Label, ...], value: float, ts_ms: int) -> None:
        labels = (Label("__name__", name),) + extra
        bucket.setdefault(labels, []).append(Sample(value, ts_ms))

    for row in modem_rows:
        ts_ms = int(row.ts * 1000)
        for name, extra, value in _modem_metrics(row.payload):
            emit(name, extra, value, ts_ms)

    for row in probe_rows:
        ts_ms = int(row.ts * 1000)
        for name, extra, value in _probe_metrics(row.payload):
            emit(name, extra, value, ts_ms)

    return [
        TimeSeries(labels=labels, samples=tuple(sorted(s, key=lambda x: x.timestamp_ms)))
        for labels, s in bucket.items()
    ]


# ---------- drain cycle ----------

def push_unpushed(conn: sqlite3.Connection, cfg: Config) -> int:
    """One drain cycle. Returns the number of source rows pushed."""
    modem_rows = select_unpushed(conn, "modem_sample", limit=BATCH_SIZE)
    probe_rows = select_unpushed(conn, "probe_sample", limit=BATCH_SIZE)
    if not modem_rows and not probe_rows:
        return 0

    series = build_series(modem_rows, probe_rows)
    log.info(
        "remote_write: pushing %d modem + %d probe rows -> %d series",
        len(modem_rows), len(probe_rows), len(series),
    )

    push(
        cfg.push.prometheus_url,
        cfg.push.prometheus_user,
        cfg.push.prometheus_password,
        series,
    )

    now = time.time()
    mark_pushed(conn, "modem_sample", (r.id for r in modem_rows), now)
    mark_pushed(conn, "probe_sample", (r.id for r in probe_rows), now)
    return len(modem_rows) + len(probe_rows)
