"""Triggered mtr snapshot — one-shot per-hop probe.

Run on demand (not on a schedule) when the regular probe sees end-to-end
p95 exceed a threshold. Shells out to `mtr --json` and parses the
`report.hubs` list into a flat structured payload that follows the same
SQLite-buffer-then-remote_write path as `probe_sample` and `modem_sample`.

mtr must be installed and either suid-root or have `cap_net_raw+ep` set
(see scripts/bootstrap.sh). On any failure (missing binary, timeout,
malformed JSON), `run_mtr` returns None and logs a warning — never
raises, so the supervisor loop is unaffected.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)

MTR_BIN = "mtr"


@dataclass(frozen=True)
class MtrHop:
    hop_num: int
    host: str
    loss_pct: float
    sent: int
    rtt_last: float
    rtt_avg: float
    rtt_best: float
    rtt_worst: float
    rtt_stdev: float


@dataclass(frozen=True)
class MtrSnapshot:
    target: str
    started_at: float
    hops: list[MtrHop] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_mtr(target: str, count: int = 30) -> MtrSnapshot | None:
    """Run one mtr report. Returns None on any error."""
    if shutil.which(MTR_BIN) is None:
        log.warning("mtr: binary %r not found on PATH; skipping snapshot", MTR_BIN)
        return None

    # mtr sends `count` probes per hop, ~100ms apart. A round number with
    # generous slack avoids killing a slow run that would still have
    # succeeded.
    timeout = count * 1.5 + 10.0
    started = time.time()
    try:
        proc = subprocess.run(
            [MTR_BIN, "--json", "--no-dns", "-c", str(count), target],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("mtr: timeout after %.0fs probing %s", timeout, target)
        return None
    except OSError as e:
        log.warning("mtr: failed to spawn: %s", e)
        return None

    if proc.returncode != 0:
        log.warning(
            "mtr: exit %d probing %s: %s",
            proc.returncode, target, proc.stderr.strip()[:200],
        )
        return None

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        log.warning("mtr: malformed JSON output: %s", e)
        return None

    hops = _parse_hubs(data)
    if hops is None:
        return None

    return MtrSnapshot(target=target, started_at=started, hops=hops)


def _parse_hubs(data: dict[str, Any]) -> list[MtrHop] | None:
    """Extract the hubs list from an mtr --json report.

    mtr's JSON schema (as of mtr 0.94+) wraps the hops in
    `report.hubs`, where each hub is a dict with string-typed numeric
    fields. We coerce defensively — missing keys become 0.0, unparseable
    values get logged and skipped.
    """
    try:
        hubs = data["report"]["hubs"]
    except (KeyError, TypeError):
        log.warning("mtr: report.hubs missing in JSON output")
        return None

    out: list[MtrHop] = []
    for h in hubs:
        try:
            out.append(
                MtrHop(
                    hop_num=int(h.get("count", 0)),
                    host=str(h.get("host", "???")),
                    loss_pct=float(h.get("Loss%", 0.0)),
                    sent=int(h.get("Snt", 0)),
                    rtt_last=float(h.get("Last", 0.0)),
                    rtt_avg=float(h.get("Avg", 0.0)),
                    rtt_best=float(h.get("Best", 0.0)),
                    rtt_worst=float(h.get("Wrst", 0.0)),
                    rtt_stdev=float(h.get("StDev", 0.0)),
                )
            )
        except (TypeError, ValueError) as e:
            log.warning("mtr: skipping malformed hop %r: %s", h, e)
            continue
    return out
