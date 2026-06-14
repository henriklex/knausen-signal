# knausen-signal

Cabin connectivity monitor. Runs on a Raspberry Pi on the LAN of a summer cabin ("Knausen") whose internet is served by a Zyxel LTE7460 4G/LTE router. Collects two streams of data and pushes them via Prometheus `remote_write` to a self-hosted VictoriaMetrics instance, with vmui as the dashboard frontend:

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
sudo editor /etc/knausen-signal/env   # fill router password + VictoriaMetrics URL
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
