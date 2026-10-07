# Running Aegis on a server

Aegis needs Python 3.10+ and nothing else. No database to provision, no
package to install, no container. One EC2 box with an 8&nbsp;GB root volume
runs several services comfortably for over a year.

## What gets installed

```
/opt/aegis            the checkout
/etc/aegis/services.yml  which logs to watch
/var/lib/aegis/.aegis    derived data, one SQLite file per service
```

Nothing is written outside `/var/lib/aegis`. Your log files are opened
read-only and never copied — only derived numbers are stored.

## Install

```bash
sudo useradd --system --home /var/lib/aegis --shell /usr/sbin/nologin aegis
sudo mkdir -p /opt/aegis /etc/aegis /var/lib/aegis
sudo chown aegis:aegis /var/lib/aegis

sudo git clone --depth 1 https://github.com/nathishdev-netizen/aegis-sre /opt/aegis
sudo chown -R aegis:aegis /opt/aegis
```

The `aegis` user needs read access to the logs it watches. If they are
owned by another service account, add it to that group rather than
loosening the files:

```bash
sudo usermod -aG adm aegis       # whichever group owns your logs
```

## Configure

One line per service in `/etc/aegis/services.yml`:

```yaml
flights: /var/log/tt/flights.log
hotels:  /var/log/tt/hotels.log
booking: /var/log/tt/booking.log
```

Each gets its own child process, its own port (3001, 3002, …) and its own
database file. One service crashing cannot affect the others.

An API key is optional. Put it in `/opt/aegis/.env` if you want the
explanation layer:

```
GROQ_API_KEY=...
```

Without it every verdict, detector and incident still works — only the
plain-English explanation goes quiet.

## Start

```bash
sudo cp /opt/aegis/deploy/aegis.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aegis
sudo systemctl status aegis
```

The overview is on port 3000, and each service's own dashboard is one click
from there.

## Check it is working

```bash
curl -s localhost:3000/healthz | python3 -m json.tool
```

```json
{
  "ok": true,
  "service": "aegis-supervisor",
  "watching": 3,
  "unhealthy": 0,
  "services": [
    {"name": "flights", "status": "ok", "events": 184203, "incidents": 2}
  ]
}
```

It answers **503** when any watched service is down, so a load balancer or
monitor can use it directly. Each child has its own `/healthz` too, which
reports `down` if its reader thread died and `degraded` if it is attached
but has not read anything for two minutes — a probe that only proved the
HTTP thread was alive would keep a broken process in service forever.

## Logs

Every child's output is tagged and forwarded, so one command shows all of
them:

```bash
journalctl -u aegis -f
```

```
[supervisor] flights -> http://127.0.0.1:3001 (pid 1182)
[flights] Aegis watching 'flights' -> http://127.0.0.1:3001
[supervisor] hotels exited (code -9); restarting in 2s
```

## Access from your laptop

The supervisor binds `0.0.0.0`; the children stay on localhost. **There is
no login yet**, so do not open port 3000 to the internet. Use an SSH tunnel:

```bash
ssh -L 3000:localhost:3000 -L 3001:localhost:3001 ec2-user@your-box
```

Then open `http://localhost:3000`.

## Storage

Everything except the per-minute counts is hard-capped, and those are kept
for 90 days:

| | |
|---|---|
| templates | ~200 rows |
| durations | 500 per operation |
| incident archive | 500 rows |
| per-minute counts | 90-day window |

In practice a service settles around **10–30&nbsp;MB** and stays there. The
8&nbsp;GB default root volume is far more than enough.

## Upgrading

```bash
cd /opt/aegis && sudo -u aegis git pull
sudo systemctl restart aegis
```

Restarting loses nothing: everything learned is on disk, and each service
picks up its log where it left off. `/healthz` reports the version, so you
can confirm which build is actually running.
