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

## Design

See `~/.claude/plans/the-router-you-just-glistening-puffin.md` for the approved plan with the verified router-API details.
