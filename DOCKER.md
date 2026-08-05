# Docker

Adroid ships with a Dockerfile + docker-compose.yml for single-node
deployment (matching the v0.1.0 release criteria).

## Quick start

```bash
# Build + run in background
docker compose up -d --build

# Tail logs
docker compose logs -f

# Test
curl http://localhost:7654/api/tools

# Stop
docker compose down
```

## Modes

### Mock mode (no phone needed)

```bash
docker compose run --rm adroid adroid start --bridge mock --port 7654 \
  --audit-log /data/adroid.auditlog --blob-store-dir /data/blobs
```

### ADB mode — wireless (Android 11+)

The container has `adb` installed. Pair from inside the container:

```bash
# Enter the container
docker compose exec adroid bash

# Pair with Android 11+ device (no USB needed)
adroid pair wireless --ip 192.168.1.42 --port 4321 --code 123456
adroid pair connect --ip 192.168.1.42 --port 5555

# Verify
adroid pair verify

# Exit — the runtime is already running with --bridge adb
exit
```

### ADB mode — USB (requires host adb server)

USB devices can't be passed directly to Docker. Run an adb server on
the host and let the container connect to it:

```bash
# 1. On the host (your laptop), start adb in server mode:
adb -a -P 5037 nodaemon server &

# 2. Uncomment the extra_hosts + ADB_SERVER_SOCKET lines in
#    docker-compose.yml

# 3. Start the container:
docker compose up -d
```

The container will use the host's adb server to talk to USB devices.

## Persistent data

The compose file mounts `./data/` to `/data` inside the container. This
holds:
- `adroid.auditlog` — the signed, hash-chained audit log
- `blobs/` — screenshot PNG files (content-addressed by sha256)

Back up this directory regularly. The audit log is append-only and
tamper-evident — losing it means losing the proof of what the AI did.

## Custom allowlist

Mount a custom allowlist config:

```yaml
# docker-compose.yml override
volumes:
  - ./data:/data
  - ./allowlist.json:/data/allowlist.json:ro
environment:
  - ADROID_ALLOWLIST_PATH=/data/allowlist.json
```

The allowlist file format:

```json
{
  "patterns": [
    "^pm list packages( -[0-9a-zA-Z]+)*$",
    "^getprop( [a-zA-Z0-9._]+)*$"
  ],
  "allow_compound": false,
  "max_command_length": 4096
}
```

## TLS termination

The container exposes plain HTTP on port 7654. For production, put a
reverse proxy in front:

```nginx
# nginx
server {
    listen 443 ssl;
    server_name adroid.example.com;

    ssl_certificate /etc/ssl/adroid.pem;
    ssl_certificate_key /etc/ssl/adroid.key;

    location / {
        proxy_pass http://localhost:7654;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

Or use Caddy for automatic Let's Encrypt:

```caddyfile
adroid.example.com {
    reverse_proxy localhost:7654
}
```

## Health check

The container includes a health check that hits `/api/tools`. Check status:

```bash
docker compose ps
# STATUS column should show "healthy"
```

## Logs

```bash
# Tail
docker compose logs -f adroid

# Last 100 lines
docker compose logs --tail 100 adroid

# Inspect audit log (separate from container logs — it's the signed log file)
docker compose exec adroid adroid audit show --limit 50
docker compose exec adroid adroid audit verify
```

## Resource limits

For production, add resource limits:

```yaml
# docker-compose.yml override
deploy:
  resources:
    limits:
      cpus: '2.0'
      memory: 1G
    reservations:
      cpus: '0.5'
      memory: 256M
```

## Updating

```bash
git pull
docker compose build --no-cache
docker compose up -d
```

The audit log persists across updates (it's in `./data/`).
