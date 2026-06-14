#!/usr/bin/env bash
# Idempotent installer for the Knausen Signal collector on a Debian-based Pi.
# Run as root (sudo). Safe to re-run for updates.
#
# What it does:
#   - installs system packages (python3-venv, sqlite3, git)
#   - creates a `knausen` system user
#   - clones or updates /opt/knausen-signal
#   - sets up a venv and pip-installs the package
#   - writes /etc/sysctl.d/99-knausen.conf for unprivileged ICMP
#   - installs the systemd unit
#   - creates /etc/knausen-signal/env from env.example if missing
#   - enables (but does not start) the service — start it once you've
#     filled the Prometheus credentials in /etc/knausen-signal/env

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/henriklex/knausen-signal.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/knausen-signal}"
DATA_DIR="/var/lib/knausen-signal"
ENV_DIR="/etc/knausen-signal"
SERVICE_USER="knausen"

if [[ "${EUID}" -ne 0 ]]; then
  echo "must be run as root (use sudo)" >&2
  exit 1
fi

echo "==> apt packages"
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip sqlite3 git ca-certificates mtr-tiny

echo "==> grant cap_net_raw to mtr (so the service user can run it without sudo)"
if command -v mtr >/dev/null 2>&1; then
  setcap cap_net_raw+ep "$(command -v mtr)"
else
  echo "    warning: mtr not on PATH after install; triggered snapshots will be skipped"
fi

echo "==> service user"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
SERVICE_GID="$(id -g "${SERVICE_USER}")"

echo "==> repo at ${INSTALL_DIR}"
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  git -C "${INSTALL_DIR}" fetch --quiet origin
  git -C "${INSTALL_DIR}" reset --hard origin/main
else
  git clone --quiet "${REPO_URL}" "${INSTALL_DIR}"
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

echo "==> venv + install"
sudo -u "${SERVICE_USER}" bash -c "
  cd '${INSTALL_DIR}'
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e .
"

echo "==> data dir ${DATA_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${DATA_DIR}"

echo "==> env dir ${ENV_DIR}"
install -d -o root -g "${SERVICE_USER}" -m 0750 "${ENV_DIR}"
if [[ ! -f "${ENV_DIR}/env" ]]; then
  install -m 0640 -o root -g "${SERVICE_USER}" \
    "${INSTALL_DIR}/env.example" "${ENV_DIR}/env"
  echo "    created ${ENV_DIR}/env from env.example — EDIT IT before starting!"
fi

echo "==> sysctl: unprivileged ICMP for gid ${SERVICE_GID}"
cat >/etc/sysctl.d/99-knausen.conf <<EOF
# Allow the knausen service user to open ICMP datagram sockets without root.
# Range covers the knausen GID (${SERVICE_GID}).
net.ipv4.ping_group_range = ${SERVICE_GID} ${SERVICE_GID}
EOF
sysctl -q --system

echo "==> systemd unit"
install -m 0644 \
  "${INSTALL_DIR}/systemd/knausen-signal.service" \
  /etc/systemd/system/knausen-signal.service
systemctl daemon-reload
systemctl enable knausen-signal.service >/dev/null

echo
echo "Bootstrap complete."
echo
echo "Next steps:"
echo "  1. sudo editor ${ENV_DIR}/env       # fill in Grafana Cloud + router password"
echo "  2. sudo systemctl start knausen-signal"
echo "  3. journalctl -u knausen-signal -f"
