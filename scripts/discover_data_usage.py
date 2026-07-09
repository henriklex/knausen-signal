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
import re
import sys
from urllib.parse import urljoin

import requests
import urllib3

# Allow running from the repo root without an editable install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from knausen_signal.modem import ZyxelLTE7460Client  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# We look for anything the router's own JS calls that mentions traffic /
# usage / quota / bandwidth. Grep across all bundled JS.
ACTION_NAME_RE = re.compile(r'action\s*[:=]\s*["\']([a-z_][a-z0-9_]+)["\']', re.I)
INTERESTING_RE = re.compile(r'traffic|usage|quota|volume|bandwidth|byte', re.I)
JS_HREF_RE = re.compile(r'src\s*=\s*["\']([^"\']+\.js[^"\']*)["\']', re.I)
# Some Zyxel UIs use inline HTML entry points that themselves load more JS.
HTML_ENTRYPOINTS = ["/", "/home.html", "/index.html", "/main.html"]


def _harvest_js_actions(session: requests.Session, base: str) -> set[str]:
    """Walk router HTML entrypoints, fetch every .js they reference, grep
    for `action:"..."` / `action="..."` strings, and return the ones that
    look traffic/usage/quota related."""
    js_urls: set[str] = set()
    for entry in HTML_ENTRYPOINTS:
        try:
            r = session.get(urljoin(base, entry), timeout=5)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        for m in JS_HREF_RE.finditer(r.text):
            js_urls.add(urljoin(base, m.group(1)))

    interesting: set[str] = set()
    for js in sorted(js_urls):
        try:
            r = session.get(js, timeout=5)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        for m in ACTION_NAME_RE.finditer(r.text):
            name = m.group(1)
            if INTERESTING_RE.search(name):
                interesting.add(name)
    return interesting


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

    # Step 1: scrape the router's own JS for candidate action names.
    base = f"https://{host}/"
    print("# harvesting action names from router JS bundle...")
    harvested = _harvest_js_actions(client.session, base)
    print(f"# harvested {len(harvested)} candidate action(s): "
          f"{sorted(harvested)}\n")

    # Step 2: try each one. Print full response on errno=0 so we can see
    # the response shape.
    for action in sorted(harvested):
        try:
            body = client._raw_post({"action": action, "args": {}})
        except Exception as e:
            print(f"[{action}] TRANSPORT ERROR: {e}")
            continue
        result = body.get(action, body)
        errno = result.get("errno") if isinstance(result, dict) else None
        if errno == 0 or errno is None:
            print(f"[{action}] OK errno={errno}")
            print(json.dumps(body, indent=2, sort_keys=True))
            print()
        else:
            errmsg = result.get("errmsg") if isinstance(result, dict) else None
            print(f"[{action}] errno={errno} errmsg={errmsg!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
