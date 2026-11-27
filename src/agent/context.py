from codex import CodexConfig
from pydantic import BaseModel, ConfigDict, HttpUrl, SecretStr


class Context(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    base_url: HttpUrl
    api_key: SecretStr
    allowed_hosts: list[str]
    codex: CodexConfig
