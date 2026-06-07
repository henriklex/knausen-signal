#!/usr/bin/env bash
# Wire the bundled knausen.json dashboard into vmui by adding
# -vmui.customDashboardsPath to the VictoriaMetrics systemd unit via a drop-in.
#
# Idempotent: re-run after `git pull` to push schema changes. Validates the
# restart and rolls back the drop-in if VM fails to come up, so a bad flag
# never leaves VM crashlooping.
#
# Designed to be invoked by:
#   ssh henrikl@<pi> 'sudo -u knausen git -C /opt/knausen-signal pull && \
#                     sudo bash /opt/knausen-signal/scripts/install-dashboard.sh'

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

# Back up any existing drop-in so we can revert if VM fails to come up.
BACKUP=""
if [[ -f "${DROPIN_FILE}" ]]; then
  BACKUP="$(mktemp)"
  cp "${DROPIN_FILE}" "${BACKUP}"
fi

echo "==> writing systemd drop-in: ${DROPIN_FILE}"
install -d -m 0755 "${DROPIN_DIR}"
cat >"${DROPIN_FILE}" <<UNIT
[Service]
# Clear the base ExecStart, then redefine with the dashboard path appended.
ExecStart=
ExecStart=/usr/local/bin/victoria-metrics \\
  -storageDataPath=/var/lib/victoria-metrics \\
  -httpListenAddr=0.0.0.0:8428 \\
  -retentionPeriod=12 \\
  -vmui.customDashboardsPath=${REPO_DIR}/dashboards
UNIT

echo "==> reload + restart victoria-metrics"
systemctl daemon-reload
systemctl restart victoria-metrics

# Wait for the unit to actually come up — restart returns before the process
# is healthy. Poll for ~15s.
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  sleep 1
  if curl -fsS -o /dev/null --max-time 1 http://localhost:8428/health; then
    echo "OK: victoria-metrics is active and serving"
    # Custom dashboards are exposed at /vmui/custom-dashboards as one JSON
    # blob containing every loaded dashboard's title + rows — not via the
    # bundled /vmui/dashboards/index.js list. Confirm our title is in there.
    if curl -fsS --max-time 2 http://localhost:8428/vmui/custom-dashboards \
        | grep -q '"Knausen'; then
      echo "OK: Knausen dashboard is loaded"
    else
      echo "WARN: Knausen dashboard not in /vmui/custom-dashboards — check JSON syntax"
    fi
    echo
    echo "Open via the public tunnel and navigate to:"
    echo "  hamburger menu (top-left) → Dashboards"
    echo "Reload the page once if you already had vmui open before this install."
    exit 0
  fi
done

# VM never came up — roll back.
echo "FAIL: victoria-metrics did not become healthy. Rolling back drop-in." >&2
if [[ -n "${BACKUP}" ]]; then
  cp "${BACKUP}" "${DROPIN_FILE}"
  rm -f "${BACKUP}"
else
  rm -f "${DROPIN_FILE}"
fi
systemctl daemon-reload
systemctl restart victoria-metrics
echo "Reverted to previous config. Recent journal:" >&2
journalctl -u victoria-metrics -n 15 --no-pager >&2
exit 1
