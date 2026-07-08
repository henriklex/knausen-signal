#!/usr/bin/env python3
"""Discover the Zyxel LTE7460 JSON action that returns data-usage.

Run this once on the Pi (or any host on the cabin LAN):

    KNAUSEN_MODEM_PASSWORD=... python3 scripts/discover_data_usage.py

It logs in, tries a battery of plausible action names against
/cgi-bin/gui.cgi, and prints anything that comes back with errno=0 and a
non-empty result. Paste the output back and I'll wire up the real code.
"""
from __future__ import annotations

import json
import os
import sys

# Allow running from the repo root without an editable install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from knausen_signal.modem import ZyxelLTE7460Client  # noqa: E402

CANDIDATE_ACTIONS = [
    # Home-page widget names — the DOM ids are `home_internet_traffic_total`
    # and `home_internet_quota`, so these are the most likely.
    "get_home_internet_traffic",
    "get_home_internet_quota",
    "get_home_internet_content",
    # Generic traffic / usage names that other Zyxel firmwares use.
    "get_traffic_status",
    "get_traffic_statistic",
    "get_traffic_statistics",
    "get_wwan_traffic_statistic",
    "get_wwan_data_usage",
    "get_wwan_traffic",
    "get_data_usage",
    "get_data_usage_status",
    "get_wan_traffic_status",
    "get_wan_traffic_statistic",
    "get_lte_traffic_status",
    "get_lte_data_usage",
    "get_monthly_traffic",
    "get_bandwidth_management_status",
    "get_bandwidth_control_status",
    # Companion "settings" endpoints — sometimes contain the monthly cap.
    "get_traffic_management_setting",
    "get_data_usage_setting",
]


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

    for action in CANDIDATE_ACTIONS:
        try:
            body = client._raw_post({"action": action, "args": {}})
        except Exception as e:
            print(f"[{action}] TRANSPORT ERROR: {e}")
            continue
        result = body.get(action, body)
        errno = result.get("errno") if isinstance(result, dict) else None
        if errno == 0 or errno is None:
            # Print the whole response so we can see the field names.
            print(f"[{action}] OK errno={errno}")
            print(json.dumps(body, indent=2, sort_keys=True))
            print()
        else:
            errmsg = result.get("errmsg") if isinstance(result, dict) else None
            print(f"[{action}] errno={errno} errmsg={errmsg!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
