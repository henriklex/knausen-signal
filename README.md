# knausen-signal

Cabin connectivity monitor. Runs on a Raspberry Pi on the LAN of a summer cabin ("Knausen") whose internet is served by a Zyxel LTE7460 4G/LTE router. Collects two streams of data and pushes them via Prometheus `remote_write` to a self-hosted VictoriaMetrics instance, with vmui as the dashboard frontend:

1. **Modem data** — signal-quality values (RSRP, RSRQ, SNR, RSSI), cell context (operator, band, EARFCN, TAC, CID, PCI), and monthly data usage (tx / rx bytes for the current billing cycle). Fetched over SSH → `JsonClient` against a Unix socket on the router (no httpd, no login flow, no session-lockout attack surface).
2. **Quality probe** — small ping / DNS / TLS / HTTPS-HEAD latency samples that fit inside the cabin's tight LTE data cap.

Everything is buffered locally in SQLite first, then pushed via Prometheus `remote_write`, so a WAN outage (the thing we most want to see) doesn't lose the data covering it.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Modem client one-shot (local dev)

```bash
export KNAUSEN_MODEM_HOST=192.168.1.1
export KNAUSEN_MODEM_SSH_KEY=~/.ssh/id_ed25519_router
python -m knausen_signal.modem
```

Requires that the router already trusts your pubkey (see router setup below).

## One-time router setup (SSH pubkey)

The collector uses SSH to invoke `JsonClient` on the router directly. Install its pubkey once:

```bash
# On the Pi, as the knausen service user. The key must live under
# /var/lib/knausen-signal/ because the systemd unit sets ProtectHome=true —
# /home is invisible to the service process even for its own user.
sudo -u knausen mkdir -p /var/lib/knausen-signal/.ssh
sudo -u knausen chmod 700 /var/lib/knausen-signal/.ssh
sudo -u knausen ssh-keygen -t ed25519 -N '' \
    -f /var/lib/knausen-signal/.ssh/id_ed25519_router \
    -C 'knausen@tsveierland -> LTE7460'
PUB=$(sudo -u knausen cat /var/lib/knausen-signal/.ssh/id_ed25519_router.pub)

# The router's telnet is publickey-less; use it once to seed the SSH key.
# Log in as admin (same password used for the web UI), then:
mkdir -p /data/user/ssh && chmod 700 /data/user/ssh
echo "$PUB" >> /data/user/ssh/authorized_keys
chmod 600 /data/user/ssh/authorized_keys
```

`/data` is on a persistent UBIFS partition — the key survives reboots and firmware upgrades that preserve user data. `sshd_config` on this firmware already has `PubkeyAuthentication yes` and `PermitRootLogin without-password`; no config edits needed.

## Deploy to the Pi

```bash
ssh pi@cabin
curl -L https://raw.githubusercontent.com/henriklex/knausen-signal/main/scripts/bootstrap.sh | sudo bash
sudo editor /etc/knausen-signal/env   # fill VictoriaMetrics URL
# Do the router SSH setup above if not already done.
sudo systemctl start knausen-signal
journalctl -u knausen-signal -f
```

Updates: `sudo bash /opt/knausen-signal/scripts/bootstrap.sh && sudo systemctl restart knausen-signal`.

## Architecture

```
[Zyxel LTE7460]  --LAN-->  [RPi]                  [Internet]  -->  [VictoriaMetrics + vmui]
                              |                                             ^
                              ├─ modem.poll   (every KNAUSEN_MODEM_INTERVAL_SEC)
                              ├─ probe.run    (every KNAUSEN_PROBE_INTERVAL_SEC)
                              ├─ SQLite buffer (long-term archive, never auto-pruned)
                              └─ push worker  (drain unpushed rows ─────────►
```

The local SQLite buffer is also the long-term archive — it's never auto-pruned, so post-mortems remain possible by reading the file off the Pi regardless of what retention the remote VM is configured with.
