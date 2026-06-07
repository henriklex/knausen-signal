"""Low-bandwidth internet-quality probe.

Produces one `ProbeSample` per call, ~12-15 KB of WAN traffic at default
settings:
    - 10 ICMP echos * 3 anycast targets       (~2 KB)
    - 1 DNS A-record lookup                    (~200 B)
    - 1 TCP connect                            (~100 B)
    - 1 TLS handshake                          (~5 KB)
    - 1 HTTPS HEAD request                     (~5 KB)

Each sub-probe is isolated: a failure in one (e.g. ICMP blocked) sets that
field to None and `probe_ok` to False but does not poison the others.
"""

from __future__ import annotations

import http.client
import logging
import math
import socket
import ssl
import time
from dataclasses import asdict, dataclass
from typing import Any

import certifi
import dns.resolver
from icmplib import ping as icmp_ping

log = logging.getLogger(__name__)

DEFAULT_PING_TARGETS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")
DEFAULT_PING_COUNT = 10
DEFAULT_PING_INTERVAL_SEC = 0.2
DEFAULT_PING_TIMEOUT_SEC = 1.0

DNS_HOSTNAME = "cloudflare.com"
TCP_TARGET = ("1.1.1.1", 443)
TLS_HOSTNAME = "cloudflare.com"
HTTPS_HOST = "cloudflare.com"
HTTPS_PATH = "/"
NET_TIMEOUT_SEC = 5.0


@dataclass(frozen=True)
class ProbeSample:
    """One sample. None = the sub-probe raised; the value 0.0/100.0 etc. means
    the sub-probe completed and that was the measurement.

    `probe_ok` reports probe-code health: True iff every sub-probe completed
    without an exception. It does NOT report internet health — use the actual
    metric values (e.g. ping_loss_pct, https_head_ms) for that.
    """
    ping_rtt_ms_p50: float | None
    ping_rtt_ms_p95: float | None
    ping_loss_pct: float | None
    dns_lookup_ms: float | None
    tcp_connect_ms: float | None
    tls_handshake_ms: float | None
    https_head_ms: float | None
    probe_ok: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_probe(
    *,
    ping_targets: tuple[str, ...] | list[str] = DEFAULT_PING_TARGETS,
    ping_count: int = DEFAULT_PING_COUNT,
    privileged_ping: bool = False,
) -> ProbeSample:
    """Run every sub-probe once. Sub-probe failures are isolated."""
    ok = True

    ping_p50, ping_p95, ping_loss = _safe(
        lambda: _ping_aggregate(ping_targets, ping_count, privileged_ping),
        "ping",
        default=(None, None, None),
    )
    if ping_p50 is None and ping_loss is None:
        ok = False

    dns_ms = _safe(lambda: _time_dns(DNS_HOSTNAME), "dns")
    tcp_ms = _safe(lambda: _time_tcp(*TCP_TARGET), "tcp")
    tls_ms = _safe(lambda: _time_tls(TLS_HOSTNAME), "tls")
    https_ms = _safe(lambda: _time_https_head(HTTPS_HOST, HTTPS_PATH), "https")

    if any(v is None for v in (dns_ms, tcp_ms, tls_ms, https_ms)):
        ok = False

    return ProbeSample(
        ping_rtt_ms_p50=ping_p50,
        ping_rtt_ms_p95=ping_p95,
        ping_loss_pct=ping_loss,
        dns_lookup_ms=dns_ms,
        tcp_connect_ms=tcp_ms,
        tls_handshake_ms=tls_ms,
        https_head_ms=https_ms,
        probe_ok=ok,
    )


def _safe(fn, name: str, default=None):
    try:
        return fn()
    except Exception as e:
        log.warning("probe sub-step %s failed: %s", name, e)
        return default


# ---------- sub-probes ----------

def _ping_aggregate(
    targets: tuple[str, ...] | list[str],
    count: int,
    privileged: bool,
) -> tuple[float | None, float | None, float | None]:
    """Run `count` pings against each target, return (p50, p95, worst-case loss%).

    p50/p95 are computed over the pooled RTTs from all targets; loss % is the
    worst single-target loss (so a single dead path is visible).
    """
    pooled_rtts: list[float] = []
    worst_loss = 0.0
    for t in targets:
        host = icmp_ping(
            t,
            count=count,
            interval=DEFAULT_PING_INTERVAL_SEC,
            timeout=DEFAULT_PING_TIMEOUT_SEC,
            privileged=privileged,
        )
        pooled_rtts.extend(host.rtts)
        # icmplib reports packet_loss as a 0..1 fraction
        worst_loss = max(worst_loss, host.packet_loss)
    p50 = percentile(pooled_rtts, 50) if pooled_rtts else None
    p95 = percentile(pooled_rtts, 95) if pooled_rtts else None
    return p50, p95, worst_loss * 100.0


def _time_dns(hostname: str) -> float:
    resolver = dns.resolver.Resolver()
    resolver.lifetime = NET_TIMEOUT_SEC
    start = time.perf_counter()
    resolver.resolve(hostname, "A")
    return (time.perf_counter() - start) * 1000.0


def _time_tcp(host: str, port: int) -> float:
    start = time.perf_counter()
    s = socket.create_connection((host, port), timeout=NET_TIMEOUT_SEC)
    elapsed = (time.perf_counter() - start) * 1000.0
    s.close()
    return elapsed


def _time_tls(hostname: str, port: int = 443) -> float:
    ctx = ssl.create_default_context(cafile=certifi.where())
    raw = socket.create_connection((hostname, port), timeout=NET_TIMEOUT_SEC)
    try:
        start = time.perf_counter()
        wrapped = ctx.wrap_socket(raw, server_hostname=hostname)
        elapsed = (time.perf_counter() - start) * 1000.0
        wrapped.close()
    finally:
        try:
            raw.close()
        except OSError:
            pass
    return elapsed


def _time_https_head(host: str, path: str) -> float:
    ctx = ssl.create_default_context(cafile=certifi.where())
    conn = http.client.HTTPSConnection(host, timeout=NET_TIMEOUT_SEC, context=ctx)
    try:
        start = time.perf_counter()
        conn.request("HEAD", path)
        resp = conn.getresponse()
        resp.read()  # drain (empty for HEAD but defensive)
        elapsed = (time.perf_counter() - start) * 1000.0
    finally:
        conn.close()
    return elapsed


# ---------- percentile helper ----------

def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile. Empty input is the caller's problem."""
    if not values:
        raise ValueError("percentile of empty input")
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(s[int(k)])
    return float(s[lo] + (s[hi] - s[lo]) * (k - lo))


def _main() -> int:
    import json

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    sample = run_probe()
    print(json.dumps(sample.as_dict(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
