#!/usr/bin/env bash
# Scaffold a sibling project from knausen-signal as a template.
#
# Copies all tracked files into a new sibling directory, strips the git
# history, renames the python package + metric prefix + service user +
# bootstrap paths to the new location, replaces modem.py with a stub
# (since the modem layer is the only genuinely project-specific code),
# and prints a checklist of what still needs human attention.
#
# Why this exists: probe / mtr / push / db / remote_write / supervisor /
# dashboard schema / SQLite buffer / heartbeat are all ISP-agnostic and
# fully reusable for another location. Only the modem client and the
# checkpoint IPs differ. The cost of two copies is small until there's
# a third deployment — at which point extract a shared library.
#
# Usage:
#     scripts/scaffold-sibling.sh <package_name> [<location_label>] [<target_dir>]
#
# Examples:
#     scripts/scaffold-sibling.sh home_signal
#         → produces ../home-signal/, location="home", env vars HOME_*
#     scripts/scaffold-sibling.sh husabo_signal husabo
#         → produces ../husabo-signal/, location="husabo", env vars HUSABO_*
#     scripts/scaffold-sibling.sh home_signal home /tmp/home-signal
#         → produces /tmp/home-signal/, location="home"

set -euo pipefail

NEW_PKG="${1:-}"
if [[ -z "$NEW_PKG" ]]; then
  sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 1
fi
if [[ "$NEW_PKG" != *"_signal" ]]; then
  echo "warn: convention is <location>_signal; you picked '$NEW_PKG' (continuing)" >&2
fi

# Derived: dash-form ("home-signal") for dirs / repo names / unit names,
# and location label ("home") for metric prefix / service user / env vars.
NEW_DASH="${NEW_PKG//_/-}"
LOCATION="${2:-${NEW_PKG%_signal}}"
LOCATION_UPPER="$(echo "$LOCATION" | tr '[:lower:]' '[:upper:]')"

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_DIR="${3:-$(cd "$SRC_DIR/.." && pwd)/${NEW_DASH}}"

if [[ -e "$TARGET_DIR" ]]; then
  echo "refusing to clobber existing target: $TARGET_DIR" >&2
  exit 1
fi

if ! git -C "$SRC_DIR" diff --quiet || ! git -C "$SRC_DIR" diff --cached --quiet; then
  echo "warn: working tree at $SRC_DIR has uncommitted changes — scaffold uses HEAD only" >&2
fi

echo "==> scaffolding $TARGET_DIR"
echo "    package:        $NEW_PKG"
echo "    dash-form:      $NEW_DASH   (repo / dir / systemd unit)"
echo "    location label: $LOCATION   (metric prefix, service user)"
echo "    env prefix:     ${LOCATION_UPPER}_*"
echo

mkdir -p "$TARGET_DIR"

# Copy only tracked files — no .venv, no __pycache__, no .git, no untracked junk.
git -C "$SRC_DIR" archive HEAD | tar -xC "$TARGET_DIR"

cd "$TARGET_DIR"

echo "==> renaming package + dashboard"
mv knausen_signal "$NEW_PKG"
mv dashboards/knausen.json "dashboards/${LOCATION}.json"

echo "==> rewriting in-file references"
# Order matters: do dash-form before plain "knausen" so we don't double-rewrite.
# Also handle Title-case Knausen separately (README headings, comments).
FILES_TO_REWRITE=$(
  find . -type f \
    \( -name '*.py' -o -name '*.json' -o -name '*.sh' -o -name '*.toml' \
       -o -name '*.example' -o -name '*.md' -o -name '*.service' \
       -o -name 'Makefile' \) \
    -not -path './.git/*'
)
# Three passes via perl (portable on mac+linux, unlike `sed -i`):
echo "$FILES_TO_REWRITE" | xargs perl -pi \
  -e "s/knausen_signal/${NEW_PKG}/g;" \
  -e "s/knausen-signal/${NEW_DASH}/g;" \
  -e "s/KNAUSEN_/${LOCATION_UPPER}_/g;" \
  -e "s/knausen/${LOCATION}/g;" \
  -e "s/Knausen/$(echo "${LOCATION:0:1}" | tr '[:lower:]' '[:upper:]')${LOCATION:1}/g;"

echo "==> stubbing modem.py (the only genuinely project-specific module)"
cat > "${NEW_PKG}/modem.py" <<EOF
"""Modem client — TODO: implement for this site's modem.

The Zyxel LTE7460 client in knausen-signal at knausen_signal/modem.py is
the reference shape: a class with poll() returning a frozen dataclass.
Replace this stub with the equivalent for whatever modem lives here.

For a DOCSIS coax modem (Sagemcom / Hitron / etc.), the data of interest
is typically exposed as JSON behind the modem's web admin UI:
  - per-channel downstream: frequency, power_dbm, snr_db, modulation
  - per-channel upstream: frequency, power_dbm, modulation
  - per-channel error counters: unerrored / correctable / uncorrectable
  - operational state (init sequence flags, link state)
  - device info: docsis_version, ip, mac, gateway

Find the JSON endpoints via the browser devtools Network panel rather
than HTML-scraping the rendered tables — HTML breaks on firmware
updates, JSON endpoints are stable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ModemSample:
    """One sample of modem state. Fields are project-specific — replace.

    Convention: None means "this sub-probe raised"; a numeric value
    (including 0) means it was measured. Same convention as ProbeSample.
    """
    connected: bool
    # TODO: add the per-channel and overall modem metrics for this site.
    # Examples for DOCSIS:
    #   ds_channel_count: int | None
    #   ds_power_dbm_min: float | None
    #   ds_snr_db_min: float | None
    #   us_power_dbm_max: float | None
    #   uncorrectable_total: int | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ModemClient:
    """Replace with the real client. poll() must return a ModemSample.

    Raise a custom lockout exception (mirror ZyxelLockoutError) if your
    modem rate-limits logins, so the supervisor can back off cleanly.
    """

    def __init__(self, host: str, username: str, password: str) -> None:
        self.host = host
        self.username = username
        self.password = password

    def poll(self) -> ModemSample:
        raise NotImplementedError("implement poll() for this site's modem")
EOF

echo "==> dropping Zyxel-specific test fixtures + modem test"
rm -f tests/fixtures/login_*.json tests/fixtures/wwan_*.json tests/test_modem.py
# Stub a placeholder so the test directory isn't empty and CI doesn't break.
cat > tests/test_modem.py <<EOF
"""TODO: tests for the new modem client."""

import pytest


@pytest.mark.skip(reason="modem client not implemented yet")
def test_modem_client_placeholder():
    pass
EOF

echo "==> noting which main.py reference needs to change"
# main.py imports ZyxelLTE7460Client + ZyxelLockoutError by name — those
# classes don't exist in the stub. Leave it broken on purpose so the
# user MUST fix it before the service can start; sprinkle a TODO so
# they see it.
perl -pi \
  -e 's/^(from \.modem import .*)$/$1  # TODO: update imports for the new modem client/g;' \
  "${NEW_PKG}/main.py" 2>/dev/null || true

echo "==> initializing fresh git history"
git init -q -b main
git add .
git commit -q -m "Initial scaffold from knausen-signal sibling

Mechanically renamed via scaffold-sibling.sh from henriklex/knausen-signal.
Probe, push, dashboard plumbing copied verbatim. modem.py is a stub —
implement it for this site's modem.
"

cat <<EOF

================================================================================
Scaffold complete: $TARGET_DIR
================================================================================

What was done for you (mechanical):
  ✓ Tracked files copied from knausen-signal HEAD
  ✓ Package renamed: knausen_signal/ → ${NEW_PKG}/
  ✓ Dashboard renamed: dashboards/knausen.json → dashboards/${LOCATION}.json
  ✓ Metric prefix rewritten: knausen_* → ${LOCATION}_*
  ✓ Env vars rewritten: KNAUSEN_* → ${LOCATION_UPPER}_*
  ✓ Bootstrap paths rewritten: /opt/knausen-signal → /opt/${NEW_DASH},
                                service user knausen → ${LOCATION}
  ✓ modem.py replaced with a stub
  ✓ Old Zyxel test fixtures + tests removed
  ✓ Fresh git history (no inherited cabin commits)

What you must do before this can run (modem-specific):
  1. Implement ${NEW_PKG}/modem.py for the site's actual modem.
     - Reference: knausen-signal's knausen_signal/modem.py
     - For DOCSIS coax: find JSON endpoints via browser devtools Network panel
  2. Update ${NEW_PKG}/main.py imports — the stub broke them on purpose
     so you can't accidentally start a service that can't talk to the modem.
  3. Update push.py's _MODEM_NUMERIC tuple to match the new ModemSample fields.
  4. Update ${NEW_PKG}/probe.py DEFAULT_CHECKPOINTS for this network's path
     (gateway → ISP first hop → IX → destination).
  5. Update dashboards/${LOCATION}.json: rip out LTE-specific rows
     (RSRP/RSRQ/SINR), add panels for the new modem's metrics.
  6. Update env.example with the new modem connection details + intervals.
  7. Add tests for the new modem client in tests/test_modem.py.

When the above is done, ship it:
  cd "$TARGET_DIR"
  pytest                                                # green before pushing
  gh repo create henriklex/${NEW_DASH} --private --source=. --push --remote=origin
  # then on the target RPi:
  ssh <pi>
  curl -L https://raw.githubusercontent.com/henriklex/${NEW_DASH}/main/scripts/bootstrap.sh | sudo bash
  sudo editor /etc/${NEW_DASH}/env
  sudo systemctl start ${NEW_DASH}
  sudo bash /opt/${NEW_DASH}/scripts/install-dashboard.sh

Both projects push to the same VictoriaMetrics instance with distinct
metric prefixes (knausen_* and ${LOCATION}_*), so cross-correlation
panels are possible if you want them later.
EOF
