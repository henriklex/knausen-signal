"""Tests for the hand-rolled Prometheus remote_write codec.

Protobuf wire format is deterministic, so we can spot-check known byte
sequences. The transport test uses `responses` to verify headers + that the
posted body is non-empty snappy-block data that decompresses back to our
encoded WriteRequest.
"""

from __future__ import annotations

import cramjam
import pytest
import responses

from knausen_signal.remote_write import (
    Label,
    RemoteWriteError,
    Sample,
    TimeSeries,
    _double_field,
    _embedded,
    _int64_field,
    _str_field,
    _tag,
    _varint,
    encode_label,
    encode_sample,
    encode_timeseries,
    encode_write_request,
    push,
)


# ---------- varint ----------

@pytest.mark.parametrize("n,expected", [
    (0,    b"\x00"),
    (1,    b"\x01"),
    (127,  b"\x7f"),
    (128,  b"\x80\x01"),
    (300,  b"\xac\x02"),
    (16384, b"\x80\x80\x01"),
])
def test_varint(n, expected):
    assert _varint(n) == expected


def test_tag_packs_field_and_wire():
    # field=1, wire=2 (length-delimited) → 0x0A
    assert _tag(1, 2) == b"\x0a"
    # field=2, wire=0 (varint) → 0x10
    assert _tag(2, 0) == b"\x10"
    # field=1, wire=1 (fixed64) → 0x09
    assert _tag(1, 1) == b"\x09"


# ---------- field encoders ----------

def test_str_field_encodes_tag_len_payload():
    out = _str_field(1, "hi")
    # tag (1<<3 | 2)=0x0A, len=2, "hi"
    assert out == b"\x0a\x02hi"


def test_str_field_handles_unicode():
    out = _str_field(2, "ærø")
    # 2 char + 1 char in UTF-8 = ærø → 6 bytes total
    payload = "ærø".encode("utf-8")
    assert out == b"\x12" + bytes([len(payload)]) + payload


def test_double_field_is_little_endian_ieee754():
    # tag=0x09 (field=1, wire=1), 8 bytes little-endian
    import struct
    out = _double_field(1, 1.5)
    assert out == b"\x09" + struct.pack("<d", 1.5)


def test_int64_field_negative_uses_two_complement():
    # -1 as uint64 is 0xFFFFFFFFFFFFFFFF (10-byte varint)
    out = _int64_field(2, -1)
    # tag 0x10, then 10 bytes of 0xFF...0x01
    assert out[0] == 0x10
    assert len(out) == 11


def test_embedded_prefixes_length_and_tag():
    out = _embedded(1, b"abc")
    assert out == b"\x0a\x03abc"


# ---------- label / sample / timeseries ----------

def test_encode_label_two_string_fields():
    out = encode_label(Label("foo", "bar"))
    assert out == _str_field(1, "foo") + _str_field(2, "bar")


def test_encode_sample_double_then_int64():
    out = encode_sample(Sample(1.5, 1700000000000))
    assert out == _double_field(1, 1.5) + _int64_field(2, 1700000000000)


def test_encode_timeseries_emits_labels_then_samples():
    ts = TimeSeries(
        labels=(Label("__name__", "x"),),
        samples=(Sample(1.0, 1000), Sample(2.0, 2000)),
    )
    out = encode_timeseries(ts)
    expected = (
        _embedded(1, encode_label(Label("__name__", "x")))
        + _embedded(2, encode_sample(Sample(1.0, 1000)))
        + _embedded(2, encode_sample(Sample(2.0, 2000)))
    )
    assert out == expected


def test_encode_write_request_wraps_timeseries():
    ts = TimeSeries(
        labels=(Label("__name__", "x"),),
        samples=(Sample(1.0, 1000),),
    )
    out = encode_write_request([ts])
    assert out == _embedded(1, encode_timeseries(ts))


def test_encode_write_request_empty():
    assert encode_write_request([]) == b""


# ---------- transport ----------

URL = "https://prom.example.com/api/prom/push"


@responses.activate
def test_push_sends_snappy_protobuf_with_basic_auth_and_headers():
    captured = {}

    def callback(request):
        captured["headers"] = dict(request.headers)
        captured["body"] = request.body
        return (200, {}, "")

    responses.add_callback(responses.POST, URL, callback=callback)

    ts = TimeSeries(
        labels=(Label("__name__", "test_metric"),),
        samples=(Sample(42.0, 1_700_000_000_000),),
    )
    push(URL, "user", "pass", [ts])

    h = captured["headers"]
    assert h["Content-Type"] == "application/x-protobuf"
    assert h["Content-Encoding"] == "snappy"
    assert h["X-Prometheus-Remote-Write-Version"] == "0.1.0"
    # basic auth user:pass = dXNlcjpwYXNz
    assert h["Authorization"] == "Basic dXNlcjpwYXNz"

    # Body decompresses cleanly to our encoded WriteRequest
    decompressed = bytes(cramjam.snappy.decompress_raw(captured["body"]))
    assert decompressed == encode_write_request([ts])


@responses.activate
def test_push_raises_on_non_2xx():
    responses.add(responses.POST, URL, status=400, body="bad request")
    ts = TimeSeries(
        labels=(Label("__name__", "x"),),
        samples=(Sample(1.0, 1000),),
    )
    with pytest.raises(RemoteWriteError) as ei:
        push(URL, "u", "p", [ts])
    assert "400" in str(ei.value)


def test_push_skips_when_no_series():
    # No HTTP calls registered — if push tries to call out, responses raises.
    push(URL, "u", "p", [])
