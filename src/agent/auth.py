import hmac

from langgraph_sdk import Auth

from agent.settings import settings

auth = Auth()


@auth.authenticate
async def authenticate(headers: dict[bytes, bytes]) -> str:
    if not hmac.compare_digest(headers.get(b"x-api-key", b""), settings.token.get_secret_value().encode()):
        raise Auth.exceptions.HTTPException(status_code=401, detail="Missing or invalid x-api-key header")
    return "anonymous"
