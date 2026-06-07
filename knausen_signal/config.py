"""Environment-variable configuration for knausen-signal.

All settings are read from `KNAUSEN_*` env vars. Missing required vars raise
ConfigError with a clear message naming the variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


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
        )
        if require_push:
            push = PushConfig(
                interval_sec=_int("KNAUSEN_PUSH_INTERVAL_SEC", 60),
                prometheus_url=_required("KNAUSEN_PROMETHEUS_URL"),
                prometheus_user=_required("KNAUSEN_PROMETHEUS_USER"),
                prometheus_password=_required("KNAUSEN_PROMETHEUS_PASSWORD"),
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
            push=push,
        )
