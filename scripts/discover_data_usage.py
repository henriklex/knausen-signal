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
HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+\.html?[^"\']*)["\']', re.I)
# Screenshot showed URL like /router/router_operating_mode.html — pages live
# under /router/, /internet/, etc. Start with what we know and let the crawl
# expand it.
HTML_ENTRYPOINTS = [
    "/", "/home.html", "/index.html", "/main.html", "/login.html",
    "/router/router_operating_mode.html",
]
# Directories to probe blindly if the entrypoints yield nothing.
JS_DIR_GUESSES = ["/js/", "/scripts/", "/static/", "/static/js/",
                  "/router/js/", "/router/", "/home/", "/home/js/"]


def _fetch(session: requests.Session, url: str) -> tuple[int, str]:
    try:
        r = session.get(url, timeout=5, verify=False)
        return r.status_code, r.text
    except Exception as e:
        return 0, f"# ERROR: {e}"


def _harvest_js_actions(session: requests.Session, base: str) -> set[str]:
    """Crawl router HTML entrypoints, fetch every .js they reference,
    grep for `action:"..."` / `action="..."` strings, and return the ones
    that look traffic/usage/quota related. Prints diagnostics so we can
    see where the crawl failed if it yields nothing."""
    print("# probing HTML entrypoints:")
    html_pages: dict[str, str] = {}
    for entry in HTML_ENTRYPOINTS:
        url = urljoin(base, entry)
        status, body = _fetch(session, url)
        size = len(body) if isinstance(body, str) else 0
        print(f"    {status:>3}  {size:>7}B  {url}")
        if status == 200 and size > 0:
            html_pages[url] = body

    # Expand: any .html hrefs found on those pages, add to the crawl.
    for src_url, body in list(html_pages.items()):
        for m in HREF_RE.finditer(body):
            url = urljoin(src_url, m.group(1))
            if url in html_pages:
                continue
            status, more = _fetch(session, url)
            if status == 200 and more:
                html_pages[url] = more
                print(f"    {status:>3}  {len(more):>7}B  {url}  (from crawl)")

    # Harvest .js srcs across every collected HTML page.
    js_urls: set[str] = set()
    for src_url, body in html_pages.items():
        for m in JS_HREF_RE.finditer(body):
            js_urls.add(urljoin(src_url, m.group(1)))

    # Blind fallback: many Zyxel UIs load bundle.js / main.js from /js/.
    if not js_urls:
        print("# no <script src=...> matches; trying blind guesses:")
        for d in JS_DIR_GUESSES:
            for fname in ("bundle.js", "main.js", "app.js", "home.js"):
                url = urljoin(base, d + fname)
                status, body = _fetch(session, url)
                print(f"    {status:>3}  {len(body):>7}B  {url}")
                if status == 200 and body:
                    js_urls.add(url)

    print(f"# discovered {len(js_urls)} JS file(s)")
    interesting: set[str] = set()
    for js in sorted(js_urls):
        status, body = _fetch(session, js)
        if status != 200:
            continue
        hits = [m.group(1) for m in ACTION_NAME_RE.finditer(body)]
        matches = [n for n in hits if INTERESTING_RE.search(n)]
        print(f"    {js}: {len(hits)} action mentions, "
              f"{len(matches)} interesting")
        for n in matches:
            interesting.add(n)
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
