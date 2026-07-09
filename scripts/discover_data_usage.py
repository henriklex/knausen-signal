#!/usr/bin/env python3
"""Confirm the Zyxel LTE7460 data-usage RPC responses.

Prior discovery revealed the home page uses this flow:

    1. get_wwan_pkt_threshold        -> {usage_cycle:{start_date,end_date},
                                          quota, alarm}
    2. get_wwan_total_network_stats  (args from step 1) -> {tx, rx}
    3. get_data_limit_config         -> {data_limit}

This script exercises all three and prints the raw responses so we can
verify the exact field shapes (units, string vs int, key names) before
committing parser code.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from knausen_signal.modem import ZyxelLTE7460Client  # noqa: E402


def _dump(label: str, obj: dict) -> None:
    print(f"# {label}")
    print(json.dumps(obj, indent=2, sort_keys=True))
    print()


def main() -> int:
    host = os.environ.get("KNAUSEN_MODEM_HOST", "192.168.1.1")
    user = os.environ.get("KNAUSEN_MODEM_USER", "admin")
    password = os.environ.get("KNAUSEN_MODEM_PASSWORD")
    if not password:
        print("set KNAUSEN_MODEM_PASSWORD env var", file=sys.stderr)
        return 2

    client = ZyxelLTE7460Client(host, user, password)
    client.login()
    print(f"# logged in to {host}\n")

    # 1. threshold — tells us billing cycle + quota.
    threshold_body = client._raw_post(
        {"action": "get_wwan_pkt_threshold", "args": {}}
    )
    _dump("get_wwan_pkt_threshold", threshold_body)

    # 2. get_wwan_total_network_stats — the JS always passes dates, and
    # calling with empty args returns non-JSON, so use the dates the
    # threshold call gave us.
    threshold = threshold_body.get("get_wwan_pkt_threshold", {})
    cycle = threshold.get("usage_cycle", {}) or {}
    start_date = cycle.get("start_date")
    end_date = cycle.get("end_date")
    if start_date and end_date:
        try:
            stats_body = client._raw_post({
                "action": "get_wwan_total_network_stats",
                "args": {"start_date": start_date, "end_date": end_date},
            })
            _dump(
                f"get_wwan_total_network_stats "
                f"(start={start_date!r}, end={end_date!r})",
                stats_body,
            )
        except Exception as e:
            print(f"# get_wwan_total_network_stats failed: {e}\n")
    else:
        print("# skipping stats call: threshold gave no usage_cycle\n")

    # 3. data-limit config — 0/1 whether the cap is actually enforced.
    try:
        config_body = client._raw_post(
            {"action": "get_data_limit_config", "args": {}}
        )
        _dump("get_data_limit_config", config_body)
    except Exception as e:
        print(f"# get_data_limit_config failed: {e}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
