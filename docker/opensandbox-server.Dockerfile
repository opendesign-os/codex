# syntax=docker/dockerfile:1
#
# A ready-to-run OpenSandbox lifecycle server for codex: the official server image plus a
# Docker-runtime config. codex points OPEN_SANDBOX_DOMAIN at it (default 127.0.0.1:8080).
#
#   docker build -f docker/opensandbox-server.Dockerfile -t opensandbox-server-codex docker
#   docker run --rm -p 8080:8080 \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     --add-host host.docker.internal:host-gateway \
#     opensandbox-server-codex
#
# The host Docker socket is required: the server creates each sandbox (and its injected execd
# and egress sidecar) as containers on the host. Heredoc COPY needs BuildKit (Docker 23+).

FROM opensandbox/server:release-1.1.0

# Tags from OpenSandbox's own docker-compose example. In mainland China, the mirror
# sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/{execd,egress}:<tag> is faster.
COPY <<'EOF' /etc/opensandbox/config.toml
[server]
host = "0.0.0.0"
port = 8080
# Required unless the container runs with OPENSANDBOX_INSECURE_SERVER=YES. Set it to the codex
# app's OPEN_SANDBOX_API_KEY to enforce auth; left empty, run with the insecure env for local.
api_key = ""

[runtime]
type = "docker"
execd_image = "opensandbox/execd:v1.1.0"

[egress]
image = "opensandbox/egress:v1.1.7"
# dns+nft is required by the Credential Vault (credentialProxy.enabled) the codex app turns on.
mode = "dns+nft"
readiness_timeout_seconds = 30.0

[docker]
network_mode = "bridge"
# Address clients use to reach each sandbox's published ports. On Docker Desktop the host reaches
# them at 127.0.0.1 (host.docker.internal resolves to a path that 502s from the host), so the app
# connects directly (OPEN_SANDBOX_USE_SERVER_PROXY empty). The server's own port proxy would need
# host.docker.internal instead, but the codex app connects direct, not through that proxy.
host_ip = "127.0.0.1"
port_range_min = 40000
port_range_max = 60000
no_new_privileges = true
pids_limit = 4096

[storage]
# Allowlist of host path prefixes for bind mounts; covers the codex skills dir under
# SANDBOX_SHARED_PATH (Docker Desktop maps the host under /run/desktop/mnt/host).
allowed_host_paths = ["/run/desktop/mnt/host"]
EOF

ENV SANDBOX_CONFIG_PATH=/etc/opensandbox/config.toml
EXPOSE 8080
