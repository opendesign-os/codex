from codex import CodexConfig
from codex.app_server import AppServerTurnOptions
from codex.protocol import types as protocol
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr


class Context(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    base_url: HttpUrl
    api_key: SecretStr
    allowed_hosts: list[str]
    codex: CodexConfig
    turn: AppServerTurnOptions = Field(default_factory=AppServerTurnOptions)
    tools: list[protocol.DynamicToolSpec] = Field(default_factory=list[protocol.DynamicToolSpec])
    review: protocol.ReviewTarget | None = None
