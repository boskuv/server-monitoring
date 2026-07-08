# Infrastructure Monitoring Service

A lightweight, cron-friendly service that collects host CPU, RAM, disk, and network metrics and sends a full status report to Telegram. Optionally scans Docker container logs for errors.

## Features

- **CPU**: usage %, load average (1/5/15 min), core count
- **RAM & Swap**: used/total and percentage
- **Disk**: per-mount usage with warning markers above threshold
- **Network**: per-interface throughput since last run (Mbps)
- **System**: hostname and uptime
- **Docker Logs** (optional): scan configured containers for errors since last run, with regex extraction and grouping
- **Host Logs** (optional): scan host log files (e.g. SSH auth.log) for access attempts since last run

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
| `LOG_CHECKS_ENABLED` | no | `false` | Enable Docker log error scanning |
| `LOG_CHECKS_FILE` | no | `/config/log_checks.yaml` | Path to log check rules |
| `LOG_STATE_FILE` | no | `/data/log_state.json` | Per-container last-check state |
| `LOG_DEFAULT_LOOKBACK_MINUTES` | no | `15` | First-run log lookback window |
| `REPORT_MODE` | no | `always` | `always` or `scheduled_or_alert` |
| `REPORT_DAILY_HOUR` | no | `9` | UTC hour for daily full report |
| `REPORT_DAILY_MINUTE` | no | `0` | UTC minute for daily full report |
| `REPORT_STATE_FILE` | no | `/data/report_state.json` | Daily/alert delivery state |
| `CPU_ALERT_PERCENT` | no | `0` | CPU alert threshold (`0` = off) |
| `RAM_ALERT_PERCENT` | no | `0` | RAM alert threshold |
| `DISK_ALERT_PERCENT` | no | `0` | Disk alert threshold (any mount) |
| `LOAD_ALERT_PER_CORE` | no | `0` | Load average per core threshold |
| `NET_RX_ALERT_MBPS` | no | `0` | Network RX Mbps threshold |
| `NET_TX_ALERT_MBPS` | no | `0` | Network TX Mbps threshold |
| `LOG_ERRORS_TRIGGER_ALERT` | no | `true` | Docker log errors trigger alert |
| `ALERT_COOLDOWN_MINUTES` | no | `120` | Min interval between alert messages |

## Conditional report delivery

By default every run sends a full report (`REPORT_MODE=always`). For frequent cron checks with less noise, use `scheduled_or_alert`:

```env
REPORT_MODE=scheduled_or_alert
REPORT_DAILY_HOUR=9
REPORT_DAILY_MINUTE=0

CPU_ALERT_PERCENT=90
RAM_ALERT_PERCENT=85
DISK_ALERT_PERCENT=90
LOAD_ALERT_PER_CORE=2.0
NET_RX_ALERT_MBPS=50
NET_TX_ALERT_MBPS=50
LOG_ERRORS_TRIGGER_ALERT=true
ALERT_COOLDOWN_MINUTES=120
```

Cron can run every 30 minutes — metrics and logs are **always collected**, but Telegram messages are sent only when:

1. **Daily report** — first run at or after `REPORT_DAILY_HOUR:REPORT_DAILY_MINUTE` UTC each day → **full report**
2. **Threshold alert** — CPU/RAM/disk/load/network/logs exceed configured limits → **short alert message**
3. Otherwise — silent exit 0 (`Report skipped`)

Threshold alerts respect `ALERT_COOLDOWN_MINUTES` to avoid spam. Daily reports are not affected by cooldown.

Example cron:

```cron
*/30 * * * * cd /path/to/monitoring-service && docker compose --profile monitor run --rm monitor >> /var/log/infra-monitor.log 2>&1
```

Short alert example:

```
⚠️ ALERT — myhost (2026-06-14 14:30 UTC)

• CPU 95.2% (threshold 90%)
• RAM 87.3% (threshold 85%)
• Logs backend: 5 errors
```

## Docker log monitoring

Log scanning is **disabled by default**. To enable:

1. Copy and customize the example config:

```bash
cp log_checks.yaml.example log_checks.yaml
```

2. Set `enabled: true` in `log_checks.yaml` and define your containers and/or host logs.

3. Enable in `.env`:

```env
LOG_CHECKS_ENABLED=true
```

### Example `log_checks.yaml`

```yaml
enabled: true

containers:
  - name: backend
    match: "backend"
    error_pattern: "(?i)(error|exception|traceback|critical)"
    extract:
      - label: order_id
        pattern: "order_id[=: ]+(\\d+)"
    max_samples: 2

  - name: tg-bot
    match: ".*bot.*"
    error_pattern: "(?i)error"

host_logs:
  - name: auth
    path: /host/var/log/auth.log
    error_pattern: "(?i)(failed password|invalid user|authentication failure|accepted password|accepted publickey)"
    extract:
      - label: ip
        pattern: "from ([\\d.]+) port"
      - label: user
        pattern: "for (?:invalid user )?(\\S+)"
    max_samples: 3
```

| Field | Description |
|-------|-------------|
| `name` | Display label in the Telegram report |
| `match` | Regex matched against Docker container names (containers only) |
| `path` | Absolute path to host log file inside the monitor container (host_logs only) |
| `error_pattern` | Regex — log line must match to count as an event |
| `extract` | Optional regex captures for grouping (e.g. `ip=1.2.3.4, user=root (5x)`) |
| `max_samples` | Max sample lines shown per source (default 2 for containers, 3 for host logs) |

### Host auth log (SSH access attempts)

The monitor container mounts the server root at `/host` (see `docker-compose.yml`). Auth log paths:

| OS | Host path | Path in monitor container |
|----|-----------|---------------------------|
| Debian/Ubuntu | `/var/log/auth.log` | `/host/var/log/auth.log` |
| RHEL/CentOS | `/var/log/secure` | `/host/var/log/secure` |

Add a `host_logs` entry with patterns for failed logins, invalid users, and successful SSH sessions. Events are grouped by extracted IP and username. On the first run, only the last 1 MB of the file is scanned; later runs read only new lines since the last check.

For local development without Docker, use the real host path (e.g. `/var/log/auth.log`) instead of `/host/...`.

Each cron run scans only logs **since the last check**. The scan period equals your cron interval (e.g. 15 minutes).

The report always includes a **Docker Logs** section:
- `disabled` when `LOG_CHECKS_ENABLED=false`
- `no errors` per container when clean
- grouped error counts and sample lines when errors are found

Requires read-only access to `/var/run/docker.sock` (already configured in `docker-compose.yml`).

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
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./monitoring-service/log_checks.yaml:/config/log_checks.yaml:ro
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
export LOG_CHECKS_ENABLED=true
export LOG_CHECKS_FILE=./log_checks.yaml
export LOG_STATE_FILE=./data/log_state.json
python -m monitor
```

## Verification

1. Run once — a Telegram message should arrive.
2. Compare values with `free -h`, `df -h`, and `uptime` on the host.
3. Run again after a few minutes — network throughput rates should appear.
4. Use an invalid token — the process should exit with code 1.
5. Enable log checks, trigger a test error in a container — next run shows grouped error count.
6. Second run with no new errors — container shows `no errors`.
