"""Client for the Zyxel LTE7460 4G/LTE router web API.

The router exposes a single JSON-RPC-style endpoint at /cgi-bin/gui.cgi.
Login establishes a CGISID cookie that subsequent requests reuse. Sessions
expire after ~180 s of idleness; the client transparently re-logs-in.

Three failed login attempts trigger a lockout — the response carries
errno=6 with a seconds-remaining value. The client raises ZyxelLockoutError
in that case so the caller can back off for the indicated duration instead
of hammering the device into a longer lockout.

Verified-live (2026-06) firmware: V1.00(ABFR.4)C0
"""

from __future__ import annotations

import logging
import random
import urllib3
from dataclasses import asdict, dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

# Router uses a self-signed cert on a LAN IP. There is no CA path that could
# validate it, and the IP is fixed by the LAN. Suppress the warning.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_TIMEOUT = 10.0


class ZyxelError(Exception):
    """Base for all router-client errors."""


class ZyxelAuthError(ZyxelError):
    """Login was rejected (wrong credentials, malformed request, etc.)."""


class ZyxelLockoutError(ZyxelAuthError):
    """Three failed logins triggered the router's cool-down."""

    def __init__(self, seconds_remaining: int):
        super().__init__(f"Locked out for {seconds_remaining} more seconds")
        self.seconds_remaining = seconds_remaining


class ZyxelResponseError(ZyxelError):
    """Router replied but with errno != 0 on a data call."""

    def __init__(self, action: str, errno: int, errmsg: str):
        super().__init__(f"{action}: errno={errno} errmsg={errmsg!r}")
        self.action = action
        self.errno = errno
        self.errmsg = errmsg


@dataclass(frozen=True)
class ModemSample:
    """Parsed signal snapshot. None for fields the router omitted."""
    connected: bool
    rsrp_dbm: int | None
    rsrq_db: int | None
    snr_db: int | None
    rssi_dbm: int | None
    cqi: int | None
    operator: str | None
    mcc: int | None
    mnc: int | None
    network_type: str | None  # "LTE", "LTE+", ...
    band_primary: str | None
    earfcn_primary: int | None
    band_secondary: str | None  # populated when carrier-aggregation is active
    earfcn_secondary: int | None
    tac: int | None
    cid: int | None
    pci: int | None
    ipv4_address: str | None
    ipv4_connection_time: str | None
    ipv6_address: str | None
    # Monthly data usage for the current billing cycle, in bytes. Two
    # RPCs behind the home page's "Data Usage: N GB" widget; None when
    # the router refused those calls (they're a bonus signal — a failure
    # here must not drop the primary modem sample).
    data_usage_tx_bytes: int | None = None
    data_usage_rx_bytes: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ZyxelLTE7460Client:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = False
        self._logged_in = False

    @property
    def base_url(self) -> str:
        return f"https://{self.host}/cgi-bin/gui.cgi"

    def login(self) -> None:
        body = self._raw_post(
            {"action": "set_system_user_login",
             "args": {"name": self.username, "password": self.password}}
        )
        result = body.get("set_system_user_login", {})
        errno = result.get("errno")
        if errno == 0:
            self._logged_in = True
            log.info("logged in to %s", self.host)
            return
        if errno == 6:
            # errmsg holds seconds-remaining as a string per the device JS
            try:
                seconds = int(result.get("errmsg", "0"))
            except (TypeError, ValueError):
                seconds = 60
            raise ZyxelLockoutError(seconds)
        raise ZyxelAuthError(
            f"login failed: errno={errno} errmsg={result.get('errmsg')!r}"
        )

    def poll(self) -> ModemSample:
        """Fetch the WWAN status + monthly data usage, re-logging-in once
        if the session died. Usage is best-effort — a failure there does
        not drop the primary modem sample."""
        if not self._logged_in:
            self.login()
        try:
            body = self._call("get_wwan_network_internet_status", {})
        except ZyxelResponseError as e:
            # errno != 0 on a data call most likely means the session expired.
            # Re-login once and retry; if that also fails, let it propagate.
            log.info("data call returned errno=%s, re-logging-in", e.errno)
            self._logged_in = False
            self.login()
            body = self._call("get_wwan_network_internet_status", {})
        tx_bytes, rx_bytes = self._fetch_data_usage()
        return self._parse_status(body, tx_bytes=tx_bytes, rx_bytes=rx_bytes)

    def _fetch_data_usage(self) -> tuple[int | None, int | None]:
        """Two-step fetch mirroring the home-page flow:

            1. get_wwan_pkt_threshold  -> {usage_cycle: {start_date, end_date}, ...}
            2. get_wwan_total_network_stats  (with those dates) -> {tx, rx}

        Returns (tx_bytes, rx_bytes). Any failure returns (None, None);
        we don't want a flaky usage endpoint to drop the primary modem
        sample the caller cares about.
        """
        try:
            threshold = self._call("get_wwan_pkt_threshold", {})
            cycle = threshold.get("usage_cycle") or {}
            start_date = cycle.get("start_date")
            end_date = cycle.get("end_date")
            if not start_date or not end_date:
                log.info("data-usage: threshold response missing usage_cycle")
                return None, None
            stats = self._call(
                "get_wwan_total_network_stats",
                {"start_date": start_date, "end_date": end_date},
            )
            # The router returns tx/rx in KiB, not bytes: divided by 1024 our
            # values matched the home-page "GB" figure exactly (116.97 GB shown
            # vs 0.114 GB computed from raw / 1024^3). Multiply by 1024 here so
            # the `_bytes` metric name stays truthful.
            tx = _int_or_none(stats.get("tx"))
            rx = _int_or_none(stats.get("rx"))
            return (
                tx * 1024 if tx is not None else None,
                rx * 1024 if rx is not None else None,
            )
        except Exception:
            log.exception("data-usage: fetch failed; leaving None")
            return None, None

    def _call(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        body = self._raw_post({"action": action, "args": args})
        result = body.get(action, {})
        errno = result.get("errno")
        if errno not in (0, None):
            raise ZyxelResponseError(action, errno, result.get("errmsg", ""))
        return result

    def _raw_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        # The cache-buster matches what the router JS does; harmless if dropped.
        url = f"{self.base_url}?_={random.random()}"
        resp = self.session.post(
            url,
            json=payload,
            timeout=self.timeout,
            headers={"Content-Type": "json"},  # matches the device JS exactly
        )
        if resp.status_code in (401, 403):
            self._logged_in = False
            raise ZyxelAuthError(f"HTTP {resp.status_code} on {payload.get('action')}")
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _parse_status(
        body: dict[str, Any],
        *,
        tx_bytes: int | None = None,
        rx_bytes: int | None = None,
    ) -> ModemSample:
        lte = body.get("lte") or {}
        # state == 3 is "connected" in the firmware we tested; also fall back to
        # checking the v4 IP since we have not enumerated the full state enum.
        state = body.get("state")
        ipv4 = body.get("ip") or None
        connected = state == 3 or (ipv4 not in (None, "", "0.0.0.0"))
        return ModemSample(
            connected=connected,
            rsrp_dbm=_int_or_none(lte.get("rsrp")),
            rsrq_db=_int_or_none(lte.get("rsrq")),
            snr_db=_int_or_none(lte.get("snr")),
            rssi_dbm=_int_or_none(lte.get("rssi")),
            cqi=_int_or_none(lte.get("cqi")),
            operator=lte.get("operator") or None,
            mcc=_int_or_none(lte.get("mcc")),
            mnc=_int_or_none(lte.get("mnc")),
            network_type=lte.get("type") or None,
            band_primary=lte.get("band") or None,
            earfcn_primary=_int_or_none(lte.get("chnel")),
            band_secondary=lte.get("band_1") or None,
            earfcn_secondary=_int_or_none(lte.get("chnel_1")),
            tac=_int_or_none(lte.get("tac")),
            cid=_int_or_none(lte.get("cid")),
            pci=_int_or_none(lte.get("pci")),
            ipv4_address=ipv4 if ipv4 not in ("0.0.0.0",) else None,
            ipv4_connection_time=body.get("connection_time") or None,
            ipv6_address=body.get("ipv6_ip") or None,
            data_usage_tx_bytes=tx_bytes,
            data_usage_rx_bytes=rx_bytes,
        )


def _int_or_none(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _main() -> int:
    import json
    from .config import Config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = Config.from_env(require_push=False)
    client = ZyxelLTE7460Client(cfg.modem.host, cfg.modem.username, cfg.modem.password)
    sample = client.poll()
    print(json.dumps(sample.as_dict(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
