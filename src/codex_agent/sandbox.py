"""The thread's sandbox on OpenSandbox, where Codex runs.

Each assistant has one sandbox, shared by its threads and created on first use. Its entrypoint is `codex app-server`,
which listens on the sandbox's port 8080 for WebSocket clients that bring the sandbox's token. Every thread is a Codex
session of its own under its own workspace subdirectory (/data/workspace/<thread>); a per-thread marker renews on each
run, and sessions idle longer than SESSION_IDLE are reclaimed. A volume named after the assistant holds the workspaces
and Codex's home (/data/home/.codex, the sessions included), so a sandbox that expired comes back with them. The
assistant's skills directory (<shared>/.agent/skills/<assistant>) is mounted read-only as /etc/codex/skills.

The gateway key stays in this process by default: the sandbox gets a placeholder and OpenSandbox's Credential Vault
puts the real key on Codex's requests, outbound traffic denied except to the gateway and SANDBOX_ALLOWED_HOSTS. Set
OPEN_SANDBOX_INSECURE_EGRESS to pass the key straight into the sandbox instead, for local dev where the vault cannot run.
The app-server's token is an HMAC of the sandbox's scope and a per-sandbox nonce under OPEN_SANDBOX_API_KEY, stored nowhere.
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
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from opensandbox import Sandbox, SandboxManager
from opensandbox.config import ConnectionConfig
from opensandbox.models.filesystem import DirectoryListEntry, WriteEntry
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
SESSION_IDLE = timedelta(seconds=int(os.environ.get("SESSION_IDLE_SECONDS", str(7 * 24 * 3600))))
"""How long a thread's Codex session and workspace outlive its last run; runs renew them, idle ones are reclaimed."""
SHARED_PATH = os.environ.get("SANDBOX_SHARED_PATH")
"""The shared skills directory as the Docker host sees it, such as /run/desktop/mnt/host/g/workspace/.codex."""
ALLOWED_HOSTS = [host.strip() for host in os.environ.get("SANDBOX_ALLOWED_HOSTS", "").split(",") if host.strip()]
"""Hosts the sandbox may reach besides the gateway, such as package registries."""

WORKSPACE = "/data/workspace"
_MARKERS = f"{WORKSPACE}/.lastrun"
APP_SERVER_PORT = 8080
_SKILLS = "/etc/codex"
_PLACEHOLDER = "injected-by-credential-vault"
_READY_TIMEOUT = 60.0


def server() -> ConnectionConfig:
    """The OpenSandbox server; the SDK reads OPEN_SANDBOX_DOMAIN and OPEN_SANDBOX_API_KEY from the environment."""
    return ConnectionConfig(protocol=os.environ.get("OPEN_SANDBOX_PROTOCOL", "http"))


async def acquire(thread_id: str, *, assistant_id: str | None = None) -> Sandbox:
    """The assistant's running sandbox, shared by its threads; created on first use or after an idle expiry.

    Keyed by the assistant (or the thread itself for a run without one), so all of an assistant's threads share
    one sandbox and one app-server, each thread a Codex session of its own under its own workspace subdirectory.
    """
    scope = assistant_id or thread_id
    connection = server()
    manager = await SandboxManager.create(connection)
    try:
        metadata = {"agentos.project": PROJECT, "agentos.scope": scope}
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
    else:
        sandbox = await _create(scope, assistant_id, connection)
    await _gc_idle(sandbox)
    return sandbox


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
    return url, _token(metadata["agentos.scope"], metadata["agentos.nonce"])


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


def cwd_for(thread_id: str) -> str:
    """The thread's isolated workspace directory inside the shared sandbox."""
    return f"{WORKSPACE}/{thread_id}"


async def prepare_thread(sandbox: Sandbox, thread_id: str) -> tuple[str, bool]:
    """Ensure the thread's workspace, renew its idle marker, and report whether it already existed."""
    cwd = cwd_for(thread_id)
    marker = f"{_MARKERS}/{thread_id}"
    existed = False
    with suppress(Exception):
        existed = marker in await sandbox.files.get_file_info([marker])
    with suppress(Exception):
        await sandbox.files.create_directories([WriteEntry(path=_MARKERS), WriteEntry(path=cwd)])
    await sandbox.files.write_file(marker, datetime.now(UTC).isoformat())
    return cwd, existed


async def _gc_idle(sandbox: Sandbox) -> None:
    """Delete the workspace and marker of every thread idle longer than SESSION_IDLE; best effort."""
    try:
        entries = await sandbox.files.list_directory(DirectoryListEntry(path=_MARKERS))
    except Exception:
        return
    now = datetime.now(UTC)
    stale = [entry for entry in entries if now - entry.modified_at > SESSION_IDLE]
    if not stale:
        return
    with suppress(Exception):
        await sandbox.files.delete_directories([cwd_for(entry.path.rsplit("/", 1)[-1]) for entry in stale])
    with suppress(Exception):
        await sandbox.files.delete_files([entry.path for entry in stale])
    logger.info("Reclaimed %d idle Codex session(s) in sandbox %s", len(stale), sandbox.id)


async def _create(scope: str, assistant_id: str | None, connection: ConnectionConfig) -> Sandbox:
    gateway = os.environ["LITELLM_BASE_URL"]
    url = urlsplit(gateway)
    if not url.hostname or url.scheme not in ("http", "https"):
        raise RuntimeError(f"LITELLM_BASE_URL is not an http(s) URL: {gateway}")
    nonce = secrets.token_hex(16)
    metadata = {"agentos.project": PROJECT, "agentos.scope": scope, "agentos.nonce": nonce}
    if assistant_id:
        metadata["agentos.assistant"] = assistant_id
    volumes = [
        Volume(
            name="data",
            pvc=PVC(claimName=f"agentos-{PROJECT}-{scope}", createIfNotExists=True),
            mountPath="/data",
        )
    ]
    if SHARED_PATH:
        skills = Host(path=f"{SHARED_PATH}/.agent/skills/{scope}")
        volumes.append(Volume(name="skills", host=skills, mountPath=f"{_SKILLS}/skills", readOnly=True))
    token_hash = hashlib.sha256(_token(scope, nonce).encode()).hexdigest()
    # Local dev without working OpenSandbox egress (e.g. Docker Desktop, where the dns+nft egress and
    # Credential Vault MITM do not function): pass the real key straight in and drop the egress policy
    # and vault. The key then lives in the sandbox, so keep this off anywhere the vault works.
    insecure = os.environ.get("OPEN_SANDBOX_INSECURE_EGRESS", "").strip().lower() in ("1", "true", "yes")
    if insecure:
        env = {"LITELLM_API_KEY": os.environ["LITELLM_API_KEY"]}
        network_policy = None
        credential_proxy = CredentialProxyConfig(enabled=False)
    else:
        env = {"LITELLM_API_KEY": _PLACEHOLDER}
        network_policy = NetworkPolicy(
            defaultAction="deny",
            egress=[NetworkRule(action="allow", target=host) for host in (url.hostname, *ALLOWED_HOSTS)],
        )
        credential_proxy = CredentialProxyConfig(enabled=True)
    sandbox = await Sandbox.create(
        IMAGE,
        timeout=IDLE,
        ready_timeout=timedelta(seconds=120),
        env=env,
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
        network_policy=network_policy,
        credential_proxy=credential_proxy,
        connection_config=connection,
    )
    if not insecure:
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
    logger.info("Created sandbox %s for scope %s (CODEX_MODEL: %s)", sandbox.id, scope, model)
    return sandbox


def _settings(gateway: str) -> list[str]:
    """Codex's settings, as `--config` overrides: its models come from the LiteLLM gateway."""
    settings: dict[str, object] = {
        "model_provider": "litellm",
        "model_providers.litellm.name": "LiteLLM",
        "model_providers.litellm.base_url": gateway,
        "model_providers.litellm.env_key": "LITELLM_API_KEY",
    }
    if model := os.environ.get("CODEX_MODEL"):
        settings["model"] = model
    else:
        # Without a pinned model, discover the gateway's models (context windows, reasoning efforts).
        settings["model_providers.litellm.model_catalog_url"] = f"{gateway}/models"
        settings["features.api_key_model_discovery"] = True
    return [arg for key, value in settings.items() for arg in ("--config", f"{key}={json.dumps(value)}")]


def _token(scope: str, nonce: str) -> str:
    key = os.environ["OPEN_SANDBOX_API_KEY"].encode()
    return hmac.new(key, f"codex-app-server:{scope}:{nonce}".encode(), hashlib.sha256).hexdigest()
