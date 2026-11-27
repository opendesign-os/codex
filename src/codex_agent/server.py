import hashlib
import json
from typing import Final

import httpx
from opensandbox import Sandbox

from codex_agent.context import Context
from codex_agent.crypto import sign

PORT: Final = 8080
PROVIDER: Final = "litellm"
CREDENTIAL: Final = "LITELLM_API_KEY"


def token(scope: str, nonce: str) -> str:
    return sign(f"codex-app-server:{scope}:{nonce}")


def command(context: Context, scope: str, nonce: str) -> list[str]:
    digest = hashlib.sha256(token(scope, nonce).encode()).hexdigest()
    listen = ["app-server", "--listen", f"ws://0.0.0.0:{PORT}"]
    auth = ["--ws-auth", "capability-token", "--ws-token-sha256", digest]
    return ["codex", *_overrides(context), *listen, *auth]


async def url(sandbox: Sandbox) -> str:
    endpoint = await sandbox.get_endpoint(PORT)
    return f"{_scheme(sandbox, 'ws')}://{endpoint.endpoint}"


async def ready(sandbox: Sandbox) -> bool:
    endpoint = await sandbox.get_endpoint(PORT)
    async with httpx.AsyncClient(timeout=5.0, headers=endpoint.headers) as http:
        try:
            response = await http.get(f"{_scheme(sandbox, 'http')}://{endpoint.endpoint}/readyz")
        except httpx.HTTPError:
            return False
    return response.status_code == 200


def _scheme(sandbox: Sandbox, plain: str) -> str:
    return f"{plain}s" if sandbox.connection_config.protocol == "https" else plain


def _overrides(context: Context) -> list[str]:
    base = str(context.base_url).rstrip("/")
    config: dict[str, object] = {
        "model_provider": PROVIDER,
        f"model_providers.{PROVIDER}.name": "LiteLLM",
        f"model_providers.{PROVIDER}.base_url": base,
        f"model_providers.{PROVIDER}.env_key": CREDENTIAL,
    }
    if context.codex.model:
        config["model"] = context.codex.model
    else:
        config[f"model_providers.{PROVIDER}.model_catalog_url"] = f"{base}/models"
        config["features.api_key_model_discovery"] = True
    return [arg for key, value in config.items() for arg in ("--config", f"{key}={json.dumps(value)}")]
