"""Minimal Prometheus `remote_write` client.

Hand-rolls the protobuf wire format for the small WriteRequest schema so we
don't pull in `protobuf` + `grpcio-tools` for what is, in the end, a few
dozen bytes per series. Snappy compression uses `cramjam` (pure-Rust wheels
exist for arm64/armhf, so installation on a Pi is just `pip install`).

WriteRequest schema (Prometheus / Cortex / Mimir):
    message WriteRequest {
        repeated TimeSeries timeseries = 1;
    }
    message TimeSeries {
        repeated Label  labels  = 1;
        repeated Sample samples = 2;
    }
    message Label  { string name = 1; string value = 2; }
    message Sample { double value = 1; int64 timestamp = 2; }
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass

import cramjam
import requests

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2


# ---------- wire helpers ----------

def _varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _str_field(field: int, s: str) -> bytes:
    data = s.encode("utf-8")
    return _tag(field, WIRE_LEN) + _varint(len(data)) + data


def _embedded(field: int, payload: bytes) -> bytes:
    return _tag(field, WIRE_LEN) + _varint(len(payload)) + payload


def _double_field(field: int, v: float) -> bytes:
    return _tag(field, WIRE_FIXED64) + struct.pack("<d", v)


def _int64_field(field: int, v: int) -> bytes:
    if v < 0:
        v = v + (1 << 64)  # two's complement, proto3 int64 over varint
    return _tag(field, WIRE_VARINT) + _varint(v)


# ---------- model ----------

@dataclass(frozen=True)
class Label:
    name: str
    value: str


@dataclass(frozen=True)
class Sample:
    value: float
    timestamp_ms: int


@dataclass(frozen=True)
class TimeSeries:
    labels: tuple[Label, ...]
    samples: tuple[Sample, ...]


# ---------- encoders ----------

def encode_label(lab: Label) -> bytes:
    return _str_field(1, lab.name) + _str_field(2, lab.value)


def encode_sample(s: Sample) -> bytes:
    return _double_field(1, s.value) + _int64_field(2, s.timestamp_ms)


def encode_timeseries(ts: TimeSeries) -> bytes:
    parts: list[bytes] = []
    for lab in ts.labels:
        parts.append(_embedded(1, encode_label(lab)))
    for s in ts.samples:
        parts.append(_embedded(2, encode_sample(s)))
    return b"".join(parts)


def encode_write_request(series: Iterable[TimeSeries]) -> bytes:
    return b"".join(_embedded(1, encode_timeseries(t)) for t in series)


# ---------- transport ----------

class RemoteWriteError(Exception):
    pass


def push(
    url: str,
    username: str,
    password: str,
    series: list[TimeSeries],
    *,
    timeout: float = 10.0,
) -> None:
    """POST a snappy-block-compressed WriteRequest. Raises on non-2xx."""
    if not series:
        return
    payload = encode_write_request(series)
    compressed = bytes(cramjam.snappy.compress_raw(payload))
    auth = (username, password) if username or password else None
    resp = requests.post(
        url,
        data=compressed,
        auth=auth,
        headers={
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "snappy",
            "X-Prometheus-Remote-Write-Version": "0.1.0",
            "User-Agent": "knausen-signal/0.1",
        },
        timeout=timeout,
    )
    if resp.status_code >= 300:
        raise RemoteWriteError(
            f"remote_write POST returned {resp.status_code}: "
            f"{resp.text[:500] if resp.text else '(empty)'}"
        )
