#!/usr/bin/env bash
# Wire the bundled knausen.json dashboard into vmui and apply a 20-minute
# instant-query lookback so 15-min-interval metrics aren't blank between polls.
#
# Idempotent: re-run after `git pull` to push schema changes.
# Requires sudo. Designed to be invoked by:
#   ssh henrikl@<pi> 'cd /opt/knausen-signal && sudo bash scripts/install-dashboard.sh'

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/knausen-signal}"
DROPIN_DIR=/etc/systemd/system/victoria-metrics.service.d
DROPIN_FILE="${DROPIN_DIR}/knausen.conf"

if [[ "${EUID}" -ne 0 ]]; then
  echo "must be run as root (use sudo)" >&2
  exit 1
fi

if [[ ! -f "${REPO_DIR}/dashboards/knausen.json" ]]; then
  echo "missing ${REPO_DIR}/dashboards/knausen.json — did you git pull?" >&2
  exit 1
fi

echo "==> systemd drop-in: ${DROPIN_FILE}"
install -d -m 0755 "${DROPIN_DIR}"
cat >"${DROPIN_FILE}" <<UNIT
[Service]
# Clear the base ExecStart, then redefine with the dashboard path and a wider
# instant-query lookback (default is 5m; we poll every 15m so instant queries
# would otherwise return "no data" 10 out of every 15 minutes).
ExecStart=
ExecStart=/usr/local/bin/victoria-metrics \\
  -storageDataPath=/var/lib/victoria-metrics \\
  -httpListenAddr=0.0.0.0:8428 \\
  -retentionPeriod=12 \\
  -vmui.customDashboardsPath=${REPO_DIR}/dashboards \\
  -search.lookback-delta=20m
UNIT

echo "==> reload + restart victoria-metrics"
systemctl daemon-reload
systemctl restart victoria-metrics

sleep 2
systemctl is-active victoria-metrics >/dev/null && echo "OK: victoria-metrics is active"

echo "==> verify vmui sees the dashboard"
if curl -fsS http://localhost:8428/vmui/dashboards/index.js | grep -q knausen.json; then
  echo "OK: knausen.json registered with vmui"
else
  echo "WARN: dashboard not yet listed — vmui may need a hard reload in browser"
fi

echo
echo "Done. Open the dashboard via the public tunnel:"
echo "  hamburger menu (top-left) → Dashboards → 'Knausen — felt network quality'"
