"""The thread's sandbox on OpenSandbox, where Codex runs.

Each thread has one sandbox, found by the thread's id and created on the thread's first run. Its entrypoint is
`codex app-server`, which listens on the sandbox's port 8080 for WebSocket clients that bring the sandbox's token.
A volume named after the thread holds its workspace and Codex's home (/data/home/.codex, the sessions included), so
a sandbox that expired comes back with both. The shared skills directory is mounted read-only as /etc/codex, where
Codex looks for admin skills (/etc/codex/skills).

The gateway key stays in this process. The sandbox gets a placeholder, and the Credential Vault of its egress sidecar
puts the real key on Codex's requests to the gateway. Outbound traffic is denied except to the gateway and to
SANDBOX_ALLOWED_HOSTS.

The OpenSandbox server is configured in the environment: OPEN_SANDBOX_DOMAIN (its host:port), OPEN_SANDBOX_PROTOCOL
(http, the default, or https) and OPEN_SANDBOX_API_KEY. The sandboxes' endpoints use the same protocol. The
app-server's token is an HMAC of the thread and a per-sandbox nonce under that API key, so it is stored nowhere.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from opensandbox import Sandbox, SandboxManager
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import (
    PVC,
    Credential,
    CredentialAuth,
    CredentialBinding,
    CredentialMatch,
    CredentialProxyConfig,
    Host,
    InlineCredentialSource,
    NetworkPolicy,
    NetworkRule,
    SandboxFilter,
    Volume,
)

logger = logging.getLogger(__name__)

PROJECT = "codex"
IMAGE = os.environ.get("SANDBOX_IMAGE", "opensandbox-codex:0.167.0")
IDLE = timedelta(seconds=int(os.environ.get("SANDBOX_IDLE_SECONDS", "3600")))
"""How long a sandbox outlives its last run; runs renew it."""
SHARED_PATH = os.environ.get("SANDBOX_SHARED_PATH")
"""The shared skills directory as the Docker host sees it, such as /run/desktop/mnt/host/g/workspace/.codex."""
ALLOWED_HOSTS = [host.strip() for host in os.environ.get("SANDBOX_ALLOWED_HOSTS", "").split(",") if host.strip()]
"""Hosts the sandbox may reach besides the gateway, such as package registries."""

WORKSPACE = "/data/workspace"
APP_SERVER_PORT = 8080
_SKILLS = "/etc/codex"
_PLACEHOLDER = "injected-by-credential-vault"
_READY_TIMEOUT = 60.0


def server() -> ConnectionConfig:
    """The OpenSandbox server, from the environment. Unlike the SDK, this does not fall back to localhost:8080."""
    domain = os.environ.get("OPEN_SANDBOX_DOMAIN")
    if not domain:
        raise RuntimeError("Set OPEN_SANDBOX_DOMAIN to the OpenSandbox server's host:port")
    return ConnectionConfig(
        domain=domain,
        protocol=os.environ.get("OPEN_SANDBOX_PROTOCOL", "http"),
        api_key=os.environ.get("OPEN_SANDBOX_API_KEY"),
    )


async def acquire(thread_id: str, *, assistant_id: str | None = None) -> Sandbox:
    """The thread's running sandbox, created when the thread has none or has one of an older image."""
    connection = server()
    manager = await SandboxManager.create(connection)
    try:
        metadata = {"agentos.project": PROJECT, "agentos.thread": thread_id}
        page = await manager.list_sandbox_infos(SandboxFilter(states=["Running"], metadata=metadata))
        current = [info for info in page.sandbox_infos if info.image is not None and info.image.image == IMAGE]
        keep = max(current, key=lambda info: info.created_at, default=None)
        for info in page.sandbox_infos:
            if info is not keep:  # an older image, or a duplicate from a retried run
                await manager.kill_sandbox(info.id)
    finally:
        await manager.close()
    if keep is not None:
        sandbox = await Sandbox.connect(keep.id, connection_config=connection)
        await sandbox.renew(IDLE)
        return sandbox
    return await _create(thread_id, assistant_id, connection)


async def app_server(sandbox: Sandbox) -> tuple[str, str]:
    """The app-server's WebSocket URL and token, once it answers its readiness probe."""
    metadata = (await sandbox.get_info()).metadata or {}
    endpoint = await sandbox.get_endpoint(APP_SERVER_PORT)
    # The sandbox's endpoints take the protocol of its OpenSandbox server, as the SDK reaches them.
    secure = sandbox.connection_config.protocol == "https"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _READY_TIMEOUT
    async with httpx.AsyncClient(timeout=5.0, headers=endpoint.headers) as http:
        while True:
            with suppress(httpx.HTTPError):
                if (await http.get(f"{'https' if secure else 'http'}://{endpoint.endpoint}/readyz")).status_code == 200:
                    break
            if loop.time() > deadline:
                raise RuntimeError(f"codex app-server in sandbox {sandbox.id} is not ready")
            await asyncio.sleep(0.5)
    url = f"{'wss' if secure else 'ws'}://{endpoint.endpoint}"
    return url, _token(metadata["agentos.thread"], metadata["agentos.nonce"])


@asynccontextmanager
async def kept_alive(sandbox: Sandbox) -> AsyncIterator[Sandbox]:
    """The sandbox, renewed for another idle period every third of one while the block runs."""

    async def renew() -> None:
        while True:
            await asyncio.sleep(IDLE.total_seconds() / 3)
            try:
                await sandbox.renew(IDLE)
            except Exception:
                logger.exception("Failed to renew sandbox %s", sandbox.id)

    task = asyncio.create_task(renew())
    try:
        yield sandbox
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def _create(thread_id: str, assistant_id: str | None, connection: ConnectionConfig) -> Sandbox:
    gateway = os.environ["LITELLM_BASE_URL"]
    url = urlsplit(gateway)
    if not url.hostname or url.scheme not in ("http", "https"):
        raise RuntimeError(f"LITELLM_BASE_URL is not an http(s) URL: {gateway}")
    nonce = secrets.token_hex(16)
    metadata = {"agentos.project": PROJECT, "agentos.thread": thread_id, "agentos.nonce": nonce}
    if assistant_id:
        metadata["agentos.assistant"] = assistant_id
    volumes = [
        Volume(
            name="data",
            pvc=PVC(claimName=f"agentos-{PROJECT}-{thread_id}", createIfNotExists=True),
            mountPath="/data",
        )
    ]
    if SHARED_PATH:
        volumes.append(Volume(name="skills", host=Host(path=SHARED_PATH), mountPath=_SKILLS, readOnly=True))
    token_hash = hashlib.sha256(_token(thread_id, nonce).encode()).hexdigest()
    sandbox = await Sandbox.create(
        IMAGE,
        timeout=IDLE,
        ready_timeout=timedelta(seconds=120),
        env={"LITELLM_API_KEY": _PLACEHOLDER},
        metadata=metadata,
        volumes=volumes,
        entrypoint=[
            "codex",
            *_settings(gateway),
            "app-server",
            "--listen",
            f"ws://0.0.0.0:{APP_SERVER_PORT}",
            "--ws-auth",
            "capability-token",
            "--ws-token-sha256",
            token_hash,
        ],
        network_policy=NetworkPolicy(
            defaultAction="deny",
            egress=[NetworkRule(action="allow", target=host) for host in (url.hostname, *ALLOWED_HOSTS)],
        ),
        credential_proxy=CredentialProxyConfig(enabled=True),
        connection_config=connection,
    )
    try:
        await sandbox.credential_vault.create(
            credentials=[
                Credential(name="gateway", source=InlineCredentialSource(value=os.environ["LITELLM_API_KEY"]))
            ],
            bindings=[
                CredentialBinding(
                    name="gateway-api",
                    match=CredentialMatch(schemes=[url.scheme], hosts=[url.hostname]),
                    auth=CredentialAuth(type="bearer", credential="gateway"),
                )
            ],
        )
    except BaseException:
        await sandbox.kill()
        await sandbox.close()
        raise
    model = os.environ.get("CODEX_MODEL") or "none: Codex picks one from the gateway's catalog"
    logger.info("Created sandbox %s for thread %s (CODEX_MODEL: %s)", sandbox.id, thread_id, model)
    return sandbox


def _settings(gateway: str) -> list[str]:
    """Codex's settings, as `--config` overrides: its models come from the LiteLLM gateway."""
    settings: dict[str, object] = {
        "model_provider": "litellm",
        "model_providers.litellm.name": "LiteLLM",
        "model_providers.litellm.base_url": gateway,
        "model_providers.litellm.env_key": "LITELLM_API_KEY",
        # The gateway also tells Codex about its models: their context windows and reasoning efforts.
        "model_providers.litellm.model_catalog_url": f"{gateway}/models",
        "features.api_key_model_discovery": True,
    }
    if model := os.environ.get("CODEX_MODEL"):
        settings["model"] = model
    return [arg for key, value in settings.items() for arg in ("--config", f"{key}={json.dumps(value)}")]


def _token(thread_id: str, nonce: str) -> str:
    key = os.environ["OPEN_SANDBOX_API_KEY"].encode()
    return hmac.new(key, f"codex-app-server:{thread_id}:{nonce}".encode(), hashlib.sha256).hexdigest()
