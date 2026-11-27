import hashlib
import hmac

from codex_agent.settings import settings


def sign(message: str) -> str:
    return hmac.new(settings.key.get_secret_value().encode(), message.encode(), hashlib.sha256).hexdigest()
