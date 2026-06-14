"""Environment-variable configuration for knausen-signal.

All settings are read from `KNAUSEN_*` env vars. Missing required vars raise
ConfigError with a clear message naming the variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import probe as _probe


class ConfigError(ValueError):
    pass


def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ConfigError(f"Required env var {name} is not set")
    return val


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"Env var {name}={raw!r} is not an integer") from e


def _csv(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return list(default)
    return [s.strip() for s in raw.split(",") if s.strip()]


def _kv_csv(
    name: str, default: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Parse `name=value,name2=value2` into [(name, value), ...]."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return list(default)
    out: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ConfigError(
                f"Env var {name} entry {entry!r} is not a name=value pair"
            )
        k, v = entry.split("=", 1)
        out.append((k.strip(), v.strip()))
    return out


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"Env var {name}={raw!r} is not a float") from e


@dataclass(frozen=True)
class ModemConfig:
    host: str
    username: str
    password: str
    interval_sec: int


@dataclass(frozen=True)
class ProbeConfig:
    interval_sec: int
    ping_targets: list[str]
    checkpoints: list[tuple[str, str]]


@dataclass(frozen=True)
class MtrConfig:
    enabled: bool
    target: str
    trigger_p95_ms: float
    cooldown_sec: int
    probe_count: int


@dataclass(frozen=True)
class PushConfig:
    interval_sec: int
    prometheus_url: str
    prometheus_user: str
    prometheus_password: str


@dataclass(frozen=True)
class Config:
    db_path: str
    log_level: str
    modem: ModemConfig
    probe: ProbeConfig
    mtr: MtrConfig
    push: PushConfig

    @classmethod
    def from_env(cls, *, require_push: bool = True) -> "Config":
        """Build config from environment.

        When `require_push` is False, the Prometheus push credentials are
        treated as optional — useful for local dev where you only exercise
        the modem client or the probe.
        """
        modem = ModemConfig(
            host=os.environ.get("KNAUSEN_MODEM_HOST", "192.168.1.1"),
            username=os.environ.get("KNAUSEN_MODEM_USER", "admin"),
            password=_required("KNAUSEN_MODEM_PASSWORD"),
            interval_sec=_int("KNAUSEN_MODEM_INTERVAL_SEC", 900),
        )
        probe = ProbeConfig(
            interval_sec=_int("KNAUSEN_PROBE_INTERVAL_SEC", 900),
            ping_targets=_csv("KNAUSEN_PING_TARGETS", ["1.1.1.1", "8.8.8.8", "9.9.9.9"]),
            checkpoints=_kv_csv(
                "KNAUSEN_PROBE_CHECKPOINTS",
                [(name, host) for name, host in _probe.DEFAULT_CHECKPOINTS],
            ),
        )
        mtr = MtrConfig(
            enabled=_bool("KNAUSEN_MTR_ENABLED", True),
            target=os.environ.get("KNAUSEN_MTR_TARGET", "8.8.8.8"),
            trigger_p95_ms=_float("KNAUSEN_MTR_TRIGGER_P95_MS", 500.0),
            cooldown_sec=_int("KNAUSEN_MTR_COOLDOWN_SEC", 600),
            probe_count=_int("KNAUSEN_MTR_PROBE_COUNT", 30),
        )
        # URL is always required when push is enabled, but user/password
        # are optional — the self-hosted VictoriaMetrics accepts
        # unauthenticated writes; user/password only matter if the remote
        # is fronted by basic auth.
        if require_push:
            push = PushConfig(
                interval_sec=_int("KNAUSEN_PUSH_INTERVAL_SEC", 60),
                prometheus_url=_required("KNAUSEN_PROMETHEUS_URL"),
                prometheus_user=os.environ.get("KNAUSEN_PROMETHEUS_USER", ""),
                prometheus_password=os.environ.get("KNAUSEN_PROMETHEUS_PASSWORD", ""),
            )
        else:
            push = PushConfig(
                interval_sec=_int("KNAUSEN_PUSH_INTERVAL_SEC", 60),
                prometheus_url=os.environ.get("KNAUSEN_PROMETHEUS_URL", ""),
                prometheus_user=os.environ.get("KNAUSEN_PROMETHEUS_USER", ""),
                prometheus_password=os.environ.get("KNAUSEN_PROMETHEUS_PASSWORD", ""),
            )
        return cls(
            db_path=os.environ.get("KNAUSEN_DB_PATH", "/var/lib/knausen-signal/data.sqlite"),
            log_level=os.environ.get("KNAUSEN_LOG_LEVEL", "INFO"),
            modem=modem,
            probe=probe,
            mtr=mtr,
            push=push,
        )
