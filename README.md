# Infrastructure Monitoring Service

A lightweight, cron-friendly service that collects host CPU, RAM, disk, and network metrics and sends a full status report to Telegram.

## Features

- **CPU**: usage %, load average (1/5/15 min), core count
- **RAM & Swap**: used/total and percentage
- **Disk**: per-mount usage with warning markers above threshold
- **Network**: per-interface throughput since last run (Mbps)
- **System**: hostname and uptime

Runs as a one-shot container — no long-running process. Triggered by host cron via `docker compose run --rm`.

## Quick start

1. Copy the environment file and fill in your credentials:

```bash
cp .env.example .env
```

2. Run manually:

```bash
docker compose --profile monitor run --rm monitor
```

3. Add to host cron (example: every 15 minutes):

```cron
*/15 * * * * cd /path/to/monitoring-service && docker compose --profile monitor run --rm monitor >> /var/log/infra-monitor.log 2>&1
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | yes | — | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | yes | — | Target chat or group ID |
| `DISK_WARN_PERCENT` | no | `80` | Disk usage % to mark with warning |
| `STATE_FILE` | no | `/data/net_state.json` | Path for network delta state |
| `HOSTNAME_OVERRIDE` | no | auto | Override hostname in report |

## Integration with existing Compose

Add this service block to your main `docker-compose.yml` (backend + bot + db):

```yaml
services:
  monitor:
    build: ./monitoring-service
    env_file: .env
    network_mode: host
    pid: host
    volumes:
      - ./monitoring-service/data:/data
      - /:/host:ro,rslave
    profiles:
      - monitor
```

Then run from your compose directory:

```bash
docker compose --profile monitor run --rm monitor
```

If your existing `.env` uses `BOT_TOKEN` instead of `TELEGRAM_BOT_TOKEN`, add an alias:

```env
TELEGRAM_BOT_TOKEN=${BOT_TOKEN}
TELEGRAM_CHAT_ID=your_chat_id
```

## Why host mode?

A standard container only sees its own cgroup and filesystem. `network_mode: host`, `pid: host`, and a read-only `/host` root mount give the monitor access to real host `/proc` and disk mounts, so CPU, RAM, disk, and network stats reflect the server — not the container.

## Network throughput

The first run shows cumulative RX/TX totals. Subsequent runs compute Mbps deltas using a state file persisted in `./data/net_state.json`.

## Local development (without Docker)

```bash
pip install -r requirements.txt
export PYTHONPATH=src
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export STATE_FILE=./data/net_state.json
python -m monitor
```

## Verification

1. Run once — a Telegram message should arrive.
2. Compare values with `free -h`, `df -h`, and `uptime` on the host.
3. Run again after a few minutes — network throughput rates should appear.
4. Use an invalid token — the process should exit with code 1.
