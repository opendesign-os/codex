# The per-thread sandbox image OpenSandbox runs, with `codex app-server` as entrypoint.
# Build it tagged to match SANDBOX_IMAGE (default opensandbox-codex:0.167.0) and rebuild
# when codex-python's pinned openai-codex-cli-bin version changes (see uv.lock).
#
#   docker build -f docker/codex-sandbox.Dockerfile -t opensandbox-codex:0.167.0 docker
#
# execd and the egress / Credential Vault sidecar are injected by the OpenSandbox server at
# create time (from [runtime].execd_image and [egress].image in its config), so this stays a
# plain application image. The SDK overrides the entrypoint per run to carry the WS token, so
# the CMD below is only a standalone-run default.

FROM python:3.12-slim-bookworm

ARG CODEX_VERSION=0.167.0
# Override with a mirror behind a slow link, e.g. --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# codex-python locates the binary through the codex_cli_bin package; put it (and its bundled
# helpers such as ripgrep) on PATH so the `codex` entrypoint resolves. A pre-fetched wheel in
# ./wheels is installed offline (no index); with only .gitkeep there, pip falls back to PyPI.
COPY wheels /wheels
RUN if ls /wheels/*.whl >/dev/null 2>&1; then \
        pip install --no-cache-dir --no-index --find-links /wheels "openai-codex-cli-bin==${CODEX_VERSION}"; \
    else \
        pip install --no-cache-dir -i "${PIP_INDEX_URL}" "openai-codex-cli-bin==${CODEX_VERSION}"; \
    fi \
 && ln -sf "$(python -c 'from codex_cli_bin import bundled_codex_path as p; print(p())')" /usr/local/bin/codex \
 && helpers="$(python -c 'from codex_cli_bin import bundled_path_dir as d; print(d() or "")' 2>/dev/null || true)" \
 && if [ -n "$helpers" ]; then for f in "$helpers"/*; do ln -sf "$f" /usr/local/bin/; done; fi \
 && codex --version

# Codex's home holds its sessions; the thread volume mounts at /data, so sessions persist under
# /data/home/.codex and the workspace under /data/workspace.
ENV HOME=/data/home
WORKDIR /data/workspace

EXPOSE 8080
CMD ["codex", "app-server", "--listen", "ws://0.0.0.0:8080"]
