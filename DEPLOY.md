# Deploying codex

How to run codex on a Linux server. What the variables and the assistant's fields mean is in [README.md](README.md);
this file covers what a server adds.

Everything that touches Docker runs on one host. The OpenSandbox server hands out sandbox endpoints on `127.0.0.1`, so
Zorvex runs on that host too, under systemd rather than in a container:

```text
client ─▶ reverse proxy, with auth ─▶ zorvex serve, 127.0.0.1:2027 ─▶ PostgreSQL, Redis
                                        │
                                        ▼
                       OpenSandbox server, 127.0.0.1:8080 (docker compose)
                                        │
                                        ▼
                  a sandbox per assistant: codex app-server ─▶ LiteLLM gateway
```

## Requirements

- Linux with Docker Engine 25 or later and the Compose plugin 2.23.1 or later: `docker-compose.yml` uses
  `configs.content`, `dockerfile_inline` and `healthcheck.start_interval`.
- [uv](https://docs.astral.sh/uv/), which also installs Python 3.12.
- PostgreSQL, and Redis 5.0 or later.
- Network: Docker Hub, Debian and PyPI to build; the LiteLLM gateway, from the Docker host, to run.

## 1. Code

The checkout belongs to a service user, `codex` here. `uv.lock` takes `zorvex-cli` from `../zorvex`, so Zorvex is
checked out beside it, at the tag the lock was made with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
sudo useradd --system --create-home --home-dir /opt/agentos --shell /usr/sbin/nologin codex
sudo -u codex -H git clone https://github.com/opendesign-os/zorvex.git /opt/agentos/zorvex
sudo -u codex -H git -C /opt/agentos/zorvex checkout v0.0.5
sudo -u codex -H git clone https://github.com/opendesign-os/codex.git /opt/agentos/codex
cd /opt/agentos/codex && sudo -u codex -H uv sync --frozen --no-dev
```

To take Zorvex from PyPI instead, where `zorvex-cli` 0.0.5 is published, delete `[tool.uv.sources]` from
`pyproject.toml` and run `uv lock`.

## 2. Settings

`/opt/agentos/codex/.env`, owned by `codex` and `chmod 600`:

```dotenv
DATABASE_URI=postgresql://<user>:<password>@<host>:5432/<database>?options=-csearch_path%3Dcodex
REDIS_URI=redis://<host>:6379/3
OPEN_SANDBOX_DOMAIN=127.0.0.1:8080
OPEN_SANDBOX_API_KEY=<the output of openssl rand -hex 32>
SANDBOX_MOUNT_PATH=/srv/codex
SANDBOX_IDLE=PT1H
SANDBOX_RETENTION=P7D
COMPOSE_FILE=docker-compose.yml:docker-compose.server.yml
LOG_FORMAT=json
```

`zorvex serve` loads this file, and so does Docker Compose run in this directory: it gives `OPEN_SANDBOX_API_KEY` to
the OpenSandbox server, and `COMPOSE_FILE` adds the Linux override of step 4. Zorvex's own settings, such as
`BG_JOB_TIMEOUT_SECS` (the longest a run may take, an hour by default), are in Zorvex's README.

## 3. Database

```sql
CREATE SCHEMA codex;
```

The user in `DATABASE_URI` needs to create tables in it: the server applies its migrations there as it starts, and
`uv run zorvex db upgrade` applies them out of band.

## 4. OpenSandbox and the sandbox image

`docker-compose.yml` is set up for Docker Desktop on Windows. `/opt/agentos/codex/docker-compose.server.yml` adapts it
to Docker Engine on Linux:

```yaml
services:
  opensandbox-server:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  opensandbox-codex:
    build:
      args:
        http_proxy: ""
        https_proxy: ""

configs:
  opensandbox-server:
    content: |
      [server]
      eip = "127.0.0.1"

      [runtime]
      type = "docker"
      execd_image = "opensandbox/execd:v1.1.0"

      [egress]
      image = "opensandbox/egress:v1.1.7"
      mode = "dns+nft"

      [docker]
      network_mode = "bridge"
      host_ip = "host.docker.internal"
      port_range_min = 40000
      port_range_max = 49151

      [storage]
      allowed_host_paths = ["/srv/codex"]
```

| Setting | On Linux |
|---|---|
| `extra_hosts`, `host_ip` | The OpenSandbox server runs in a container and reaches the sandboxes' published ports at `host_ip`. Docker Engine defines `host.docker.internal` only through `host-gateway`. |
| `http_proxy`, `https_proxy` | `http.docker.internal:3128` is Docker Desktop's proxy. Empty, the build goes out directly; set the server's proxy if it needs one. |
| `allowed_host_paths` | `SANDBOX_MOUNT_PATH`, whose skills the sandboxes mount, in place of Docker Desktop's drive mounts. |
| `port_range_min`, `port_range_max` | The host ports the sandboxes take, two or three each; step 7 keeps them off the network. |
| `eip` | The host in the endpoints the OpenSandbox server hands out, which Zorvex on this host reaches at `127.0.0.1`. |

These values follow from OpenSandbox's settings; the project has so far run on Docker Desktop, so step 8 checks them on
a first deploy.

```bash
cd /opt/agentos/codex
sudo docker compose build opensandbox-codex
sudo docker pull opensandbox/execd:v1.1.0
sudo docker pull opensandbox/egress:v1.1.7
sudo docker compose up -d --wait opensandbox-server
curl -fsS http://127.0.0.1:8080/health
```

The build makes `opensandbox-codex:0.167.0`, the Codex that `uv.lock` pins; the pulls spare the first sandbox the wait
for the images it starts with.

## 5. The server

`/etc/systemd/system/codex.service`:

```ini
[Unit]
Description=Codex agent server (Zorvex)
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=codex
WorkingDirectory=/opt/agentos/codex
ExecStart=/opt/agentos/codex/.venv/bin/zorvex serve --host 127.0.0.1 --port 2027 --proxy-headers
Restart=on-failure
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now codex
curl -fsS 'http://127.0.0.1:2027/ok?check_db=1'
```

The first start applies the migrations and creates the system assistant `codex`. On a stop, runs get 30 seconds
(`WORKER_DRAIN_TIMEOUT`) to finish; the others go back to the queue and start their Codex turn over after the restart.
A request waiting for a human lives in the sandbox, so it outlasts the restart.

## 6. The assistant

Configure the system assistant in the database as README's *Assistants* shows, with a `base_url` that the Docker host
reaches. Its id names its skills directory:

```bash
curl -s http://127.0.0.1:2027/assistants/search -H 'Content-Type: application/json' -d '{"graph_id": "codex"}'
sudo install -d -o codex -g codex /srv/codex/.agent/skills/<assistant_id>
```

Codex's approvals and questions to a human are in README's *Human in the loop*.

## 7. Access

- **Authentication.** `zorvex.json` has no `auth.path`, so every request is one anonymous user: whoever reaches the
  server can run Codex on the gateway's key and read every thread. Keep it on `127.0.0.1` behind a reverse proxy that
  authenticates, or give Zorvex an `auth.path`. Streams need the proxy not to buffer; in nginx, with `auth_basic` or
  your SSO in the `server` block:

  ```nginx
  location / {
      proxy_pass http://127.0.0.1:2027;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_buffering off;
      proxy_read_timeout 1h;
  }
  ```

- **Firewall.** From outside, only SSH and the proxy's port. The OpenSandbox API is bound to `127.0.0.1`, but Docker
  publishes the sandboxes' ports on every interface, and its NAT rules take them past the host's INPUT rules, ufw's
  included. Drop them in `DOCKER-USER`, with the public interface in place of `eth0`, and persist the rule:

  ```bash
  sudo iptables -I DOCKER-USER -i eth0 -p tcp -m conntrack --ctorigdstport 40000:49151 --ctdir ORIGINAL -j DROP
  ```

  A cloud security group that admits only SSH and HTTPS does the same.
- **Secrets.** `.env` holds the OpenSandbox key, which also signs each sandbox's app-server token. The database holds
  each assistant's gateway key in plain text, so its access and its backups need the same care.

## 8. Check

```bash
curl -s http://127.0.0.1:2027/runs/wait -H 'Content-Type: application/json' \
  -d '{"assistant_id": "codex", "input": {"messages": [{"role": "user", "content": "Reply with only OK."}]}}'
sudo docker ps --filter ancestor=opensandbox-codex:0.167.0
```

The first run creates the assistant's sandbox and takes longer. Its last message is `OK`, and `docker ps` lists the
sandbox.

## Operations

| | |
|---|---|
| Logs | `journalctl -u codex -f`, `sudo docker compose logs -f opensandbox-server`, and a sandbox's `sudo docker logs <container>` |
| Updates | `git pull`, Zorvex at the tag `uv.lock` uses, `sudo -u codex -H uv sync --frozen --no-dev`, `sudo systemctl restart codex` |
| Backups | `pg_dump --schema=codex`, and each assistant's volume `agentos-codex-<assistant_id>`, which holds its workspaces and Codex's sessions |
| More instances | One PostgreSQL, Redis and OpenSandbox server for all. Instances on other hosts need `eip` set to the Docker host's address and the port range open to them. |

A new Codex: when `uv.lock` moves `openai-codex-cli-bin`, build that version before the restart. Sandboxes on the old
image are replaced on their next run; their volumes keep the workspaces and sessions, but requests waiting for a human
in them are lost.

```bash
version=$(sudo -u codex -H uv run --frozen python -c "from importlib.metadata import version; print(version('openai-codex-cli-bin'))")
sudo CODEX_VERSION=$version docker compose build opensandbox-codex
sudo systemctl restart codex
```

A volume's backup:

```bash
sudo docker run --rm -v agentos-codex-<assistant_id>:/data:ro -v /backup:/backup debian:bookworm-slim \
  tar czf /backup/agentos-codex-<assistant_id>.tgz -C /data .
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `required variable OPEN_SANDBOX_API_KEY is missing a value` | Run `docker compose` in `/opt/agentos/codex`, beside `.env`. |
| A sandbox fails to start on its `skills` mount | `allowed_host_paths` covers `SANDBOX_MOUNT_PATH`, and the assistant's skills directory exists. |
| Sandboxes never get ready; the egress sidecar's readiness check fails | `host_ip` and `extra_hosts`, step 4. |
| The sandbox image is missing | Build the `openai-codex-cli-bin` version that `uv.lock` pins. |
| Requests to `127.0.0.1` go through a proxy | With `HTTP_PROXY` or `HTTPS_PROXY` in the environment, add `NO_PROXY=127.0.0.1,localhost`. |
| Runs stop with connection errors to the gateway | `base_url`'s host is reachable from the Docker host; other hosts Codex needs are in `allowed_hosts`. |
| Commands fail with `bwrap: No permissions to create a new namespace` | Codex's own sandbox cannot start in a container: `sandbox_mode = "danger-full-access"`, as README says. |
