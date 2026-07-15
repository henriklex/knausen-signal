"""Client for the Zyxel LTE7460 4G/LTE router.

Talks to the router's JSON-RPC layer directly via SSH → `JsonClient`
against the local Unix socket `/dev/shm/cgi-2-sys`. This is the same
protocol the router's own web UI uses, but bypasses the httpd — no
login flow, no session state, no lockout after failed passwords, no
risk of wedging the web daemon under load.

One-time router setup (see README): place the collector's pubkey in
`/data/user/ssh/authorized_keys` on the router. `sshd_config` on this
firmware already has PubkeyAuthentication yes and PermitRootLogin
without-password, so no config changes are needed there.

Runtime: one `ssh` subprocess per RPC. SSH options set a hard timeout
so a wedged sshd can't block the poll loop. `JsonClient` prints a
plaintext frame like:

    send:
    { "action": "..." }
    read:
    { "<action>": { ... } }

We split on `read:` and parse the JSON body.

Verified-live (2026-07) firmware: V1.00(ABFR.4)C0
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0
SOCKET_PATH = "/dev/shm/cgi-2-sys"


class ZyxelError(Exception):
    """Base for router-client errors."""


class ZyxelTransportError(ZyxelError):
    """SSH or subprocess failure — network down, key rejected, timeout, malformed reply."""


class ZyxelResponseError(ZyxelError):
    """Router replied with errno != 0 on a data call."""

    def __init__(self, action: str, errno: int, errmsg: str):
        super().__init__(f"{action}: errno={errno} errmsg={errmsg!r}")
        self.action = action
        self.errno = errno
        self.errmsg = errmsg


# (action, args) -> body[action]. Injectable for tests; production is _ssh_transport.
Transport = Callable[[str, "dict[str, Any]"], "dict[str, Any]"]


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
    # Monthly data usage for the current billing cycle, in bytes. Two RPCs
    # behind the home page's "Data Usage: N GB" widget; None when the router
    # refused those calls (they're a bonus signal — a failure here must not
    # drop the primary modem sample).
    data_usage_tx_bytes: int | None = None
    data_usage_rx_bytes: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ZyxelLTE7460Client:
    def __init__(
        self,
        host: str,
        ssh_key_path: str,
        *,
        ssh_user: str = "admin",
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
    ):
        self.host = host
        self.ssh_key_path = ssh_key_path
        self.ssh_user = ssh_user
        self.timeout = timeout
        self._transport: Transport = transport or self._ssh_transport
        # --- Diagnostic hook (TEMPORARY, safe to remove) ------------------
        # Remembers which raw `lte` key-sets we've already logged, so
        # _maybe_capture_lte_shape emits the full raw object once per distinct
        # shape instead of on every poll. See that method for the why.
        self._logged_lte_keysets: set[frozenset[str]] = set()

    def poll(self) -> ModemSample:
        """Fetch modem status + best-effort data usage. Usage failure does
        not drop the primary modem sample."""
        body = self._transport("get_wwan_network_internet_status", {})
        self._maybe_capture_lte_shape(body)
        tx_bytes, rx_bytes = self._fetch_data_usage()
        return self._parse_status(body, tx_bytes=tx_bytes, rx_bytes=rx_bytes)

    def _maybe_capture_lte_shape(self, body: dict[str, Any]) -> None:
        """Diagnostic (TEMPORARY — safe to delete): log the full raw `lte`
        object once per distinct key-set.

        Why: we parse only the first secondary carrier (band_1/chnel_1), but
        this modem reports 3+ aggregated carriers whose exact firmware keys
        (band_2/chnel_2, ...) we haven't confirmed. Emitting one sample per
        new shape lets production capture a real 3CA event so the N-carrier
        parser can be built against ground truth instead of a guess.

        Purely observational: it only reads `body`, mutates nothing the
        returned sample depends on, and swallows every error — so a bug here
        can never affect a poll or drop a sample.
        """
        try:
            lte = body.get("lte")
            if not isinstance(lte, dict):
                return
            keyset = frozenset(lte.keys())
            if keyset in self._logged_lte_keysets:
                return
            self._logged_lte_keysets.add(keyset)
            log.info(
                "modem: RAW LTE SHAPE CAPTURE (new keyset, %d keys): %s",
                len(keyset),
                json.dumps(lte, default=str, sort_keys=True),
            )
        except Exception:
            log.debug("modem: lte shape capture failed", exc_info=True)

    def _fetch_data_usage(self) -> tuple[int | None, int | None]:
        """Two-step fetch mirroring the home-page flow:

            1. get_wwan_pkt_threshold  -> {usage_cycle: {start_date, end_date}, ...}
            2. get_wwan_total_network_stats  (with those dates) -> {tx, rx}   (KiB)

        Any failure returns (None, None) — a flaky usage endpoint must not
        drop the primary modem sample.
        """
        try:
            threshold = self._transport("get_wwan_pkt_threshold", {})
            cycle = threshold.get("usage_cycle") or {}
            start_date = cycle.get("start_date")
            end_date = cycle.get("end_date")
            if not start_date or not end_date:
                log.info("data-usage: threshold missing usage_cycle")
                return None, None
            stats = self._transport(
                "get_wwan_total_network_stats",
                {"start_date": start_date, "end_date": end_date},
            )
            # Router reports tx/rx in KiB; multiply by 1024 to store true bytes
            # so the `_bytes` metric name stays truthful.
            tx = _int_or_none(stats.get("tx"))
            rx = _int_or_none(stats.get("rx"))
            return (
                tx * 1024 if tx is not None else None,
                rx * 1024 if rx is not None else None,
            )
        except Exception:
            log.exception("data-usage: fetch failed; leaving None")
            return None, None

    def _ssh_transport(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        """Invoke JsonClient on the router via SSH, parse the reply,
        return body[action] or raise ZyxelTransportError/ZyxelResponseError."""
        remote_cmd = f"JsonClient {SOCKET_PATH} {action}"
        if args:
            remote_cmd += " " + shlex.quote(json.dumps(args))
        ssh_argv = [
            "ssh",
            "-i", self.ssh_key_path,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={int(self.timeout)}",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2",
            "-o", "BatchMode=yes",                # never prompt for password
            f"{self.ssh_user}@{self.host}",
            remote_cmd,
        ]
        try:
            proc = subprocess.run(
                ssh_argv,
                capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise ZyxelTransportError(
                f"ssh timed out after {self.timeout}s"
            ) from e
        except OSError as e:
            raise ZyxelTransportError(f"could not invoke ssh: {e}") from e
        if proc.returncode != 0:
            raise ZyxelTransportError(
                f"ssh exit={proc.returncode} stderr={proc.stderr.strip()!r}"
            )

        body = _parse_jsonclient_reply(proc.stdout, action)
        errno = body.get("errno")
        if errno not in (0, None):
            raise ZyxelResponseError(action, errno, body.get("errmsg", ""))
        return body

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


def _parse_jsonclient_reply(stdout: str, action: str) -> dict[str, Any]:
    """Extract body[action] from JsonClient's `send:\\n{...}\\nread:\\n{...}` frame."""
    idx = stdout.rfind("read:")
    if idx < 0:
        raise ZyxelTransportError(
            f"JsonClient output lacked 'read:' marker: {stdout!r}"
        )
    payload = stdout[idx + len("read:"):].strip()
    try:
        outer = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ZyxelTransportError(
            f"JsonClient reply was not valid JSON: {payload!r}"
        ) from e
    inner = outer.get(action)
    if not isinstance(inner, dict):
        raise ZyxelTransportError(
            f"reply lacked expected {action!r} object: {outer!r}"
        )
    return inner


def _int_or_none(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _main() -> int:
    import json as _json
    from .config import Config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = Config.from_env(require_push=False)
    client = ZyxelLTE7460Client(
        cfg.modem.host,
        cfg.modem.ssh_key_path,
        ssh_user=cfg.modem.ssh_user,
    )
    sample = client.poll()
    print(_json.dumps(sample.as_dict(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
