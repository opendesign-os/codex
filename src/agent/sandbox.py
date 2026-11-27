import asyncio
import json
import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from importlib.metadata import version
from typing import Final, Literal

from codex.app_server import AppServerInitializeOptions, AppServerWebSocketOptions, AsyncAppServerClient
from codex.app_server.transports import AsyncWebSocketTransport
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
    SandboxInfo,
    Volume,
)

from agent import server
from agent.context import Context
from agent.crypto import sign
from agent.settings import settings

logger = logging.getLogger(__name__)

PROJECT: Final = "codex"
IMAGE: Final = f"opensandbox-codex:{version('openai-codex-cli-bin')}"
PLACEHOLDER: Final = "injected-by-credential-vault"


@dataclass(frozen=True)
class Lease:
    sandbox: Sandbox
    url: str
    token: str


async def acquire(scope: str, context: Context) -> Lease:
    connection = ConnectionConfig(domain=settings.domain, api_key=settings.key.get_secret_value())
    fingerprint = _fingerprint(context)
    manager = await SandboxManager.create(connection)
    try:
        labels = {"agentos.project": PROJECT, "agentos.scope": scope}
        page = await manager.list_sandbox_infos(SandboxFilter(states=["Running"], metadata=labels))
        current = [info for info in page.sandbox_infos if _matches(info, fingerprint)]
        keep = max(current, key=lambda info: info.created_at, default=None)
        for info in page.sandbox_infos:
            if info is not keep:
                await manager.kill_sandbox(info.id)
    finally:
        await manager.close()
    if keep is None:
        nonce = secrets.token_hex(16)
        sandbox = await _create(scope, nonce, fingerprint, context, connection)
    else:
        nonce = (keep.metadata or {})["agentos.nonce"]
        sandbox = await Sandbox.connect(keep.id, connection_config=connection)
        await sandbox.renew(settings.idle)
    return Lease(sandbox, await server.url(sandbox), server.token(scope, nonce))


@asynccontextmanager
async def connect(lease: Lease) -> AsyncGenerator[AsyncAppServerClient]:
    client = AsyncAppServerClient(
        AsyncWebSocketTransport(lease.url, AppServerWebSocketOptions(bearer_token=lease.token)),
        AppServerInitializeOptions(experimental_api=True),
    )
    try:
        async with keepalive(lease.sandbox), client:
            yield client
    finally:
        await lease.sandbox.close()


@asynccontextmanager
async def keepalive(sandbox: Sandbox) -> AsyncGenerator[Sandbox]:
    async def renew() -> None:
        while True:
            await asyncio.sleep(settings.idle.total_seconds() / 3)
            try:
                await sandbox.renew(settings.idle)
            except Exception:
                logger.exception("Failed to renew sandbox %s", sandbox.id)

    task = asyncio.create_task(renew())
    try:
        yield sandbox
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def _create(scope: str, nonce: str, fingerprint: str, context: Context, connection: ConnectionConfig) -> Sandbox:
    gateway = context.base_url
    host = gateway.host
    if host is None:
        raise ValueError(f"base_url has no host: {gateway}")
    scheme: Literal["http", "https"] = "https" if gateway.scheme == "https" else "http"
    data = Volume(
        name="data", pvc=PVC(claimName=f"agentos-{PROJECT}-{scope}", createIfNotExists=True), mountPath="/data"
    )
    skills = Volume(
        name="skills",
        host=Host(path=f"{settings.mount}/.agent/skills/{scope}"),
        mountPath="/etc/codex/skills",
        readOnly=True,
    )
    sandbox = await Sandbox.create(
        IMAGE,
        timeout=settings.idle,
        ready_timeout=timedelta(minutes=2),
        health_check=server.ready,
        env={server.CREDENTIAL: PLACEHOLDER},
        metadata={
            "agentos.project": PROJECT,
            "agentos.scope": scope,
            "agentos.nonce": nonce,
            "agentos.fingerprint": fingerprint,
        },
        resource=context.resources.model_dump(),
        volumes=[data, skills],
        entrypoint=server.command(context, scope, nonce),
        network_policy=NetworkPolicy(
            defaultAction="deny",
            egress=[NetworkRule(action="allow", target=target) for target in (host, *context.allowed_hosts)],
        ),
        credential_proxy=CredentialProxyConfig(enabled=True),
        connection_config=connection,
    )
    try:
        await sandbox.credential_vault.create(
            credentials=[
                Credential(name="gateway", source=InlineCredentialSource(value=context.api_key.get_secret_value()))
            ],
            bindings=[
                CredentialBinding(
                    name="gateway",
                    match=CredentialMatch(schemes=[scheme], hosts=[host]),
                    auth=CredentialAuth(type="bearer", credential="gateway"),
                )
            ],
        )
    except BaseException:
        await sandbox.kill()
        await sandbox.close()
        raise
    logger.info("Created sandbox %s for scope %s", sandbox.id, scope)
    return sandbox


def _matches(info: SandboxInfo, fingerprint: str) -> bool:
    image = info.image.image if info.image else None
    return image == IMAGE and (info.metadata or {}).get("agentos.fingerprint") == fingerprint


def _fingerprint(context: Context) -> str:
    shape = {
        "base_url": str(context.base_url),
        "api_key": context.api_key.get_secret_value(),
        "allowed_hosts": sorted(set(context.allowed_hosts)),
        "pinned": context.codex.model is not None,
        "resources": context.resources.model_dump(),
        "mount": settings.mount,
    }
    return sign(json.dumps(shape, sort_keys=True))[:32]
