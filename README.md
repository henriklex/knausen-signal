# knausen-signal

Cabin connectivity monitor. Runs on a Raspberry Pi on the LAN of a summer cabin ("Knausen") whose internet is served by a Zyxel LTE7460 4G/LTE router. Collects two streams of data and pushes them to Grafana Cloud Free for remote dashboarding:

1. **Modem data** — signal-quality values (RSRP, RSRQ, SNR, RSSI) plus cell context (operator, band, EARFCN, TAC, CID, PCI), scraped from the router's web API.
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
export KNAUSEN_MODEM_USER=admin
export KNAUSEN_MODEM_PASSWORD='...'
python -m knausen_signal.modem
```

## Deploy to the Pi

```bash
ssh pi@cabin
curl -L https://raw.githubusercontent.com/henriklex/knausen-signal/main/scripts/bootstrap.sh | sudo bash
sudo editor /etc/knausen-signal/env   # fill router password + Grafana Cloud creds
sudo systemctl start knausen-signal
journalctl -u knausen-signal -f
```

Updates: `sudo bash /opt/knausen-signal/scripts/bootstrap.sh && sudo systemctl restart knausen-signal`.

## Architecture

```
[Zyxel LTE7460]  --LAN-->  [RPi]                  [Internet]  -->  [Grafana Cloud Free]
                              |                                             ^
                              ├─ modem.poll   (every KNAUSEN_MODEM_INTERVAL_SEC)
                              ├─ probe.run    (every KNAUSEN_PROBE_INTERVAL_SEC)
                              ├─ SQLite buffer (long-term archive, never auto-pruned)
                              └─ push worker  (drain unpushed rows ─────────►
```

Grafana Cloud Free retains metrics for ~14 days. The local SQLite buffer is also the long-term archive, so post-mortems older than 2 weeks are still possible by reading the file off the Pi.
