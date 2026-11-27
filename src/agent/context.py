import re
from typing import Annotated

from codex import CodexConfig
from codex.app_server import AppServerTurnOptions
from codex.protocol import types as protocol
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, HttpUrl, SecretStr


def _cpu(value: str) -> str:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)m?", value)
    if match is None or float(match[1]) <= 0:
        raise ValueError("cpu is a positive number of cores, such as 2, 0.5 or 500m")
    return value


def _memory(value: str) -> str:
    # Units mean the same to OpenSandbox's Docker and Kubernetes runtimes; Docker reads a lowercase m as MB.
    match = re.fullmatch(r"(\d+)(?:Ki|Mi|Gi|Ti|k|M|G|T)", value)
    if match is None or int(match[1]) == 0:
        raise ValueError("memory is a positive size with a unit, such as 512Mi, 4Gi or 4G")
    return value


class Resources(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True, coerce_numbers_to_str=True)

    cpu: Annotated[str, AfterValidator(_cpu)] = "1"
    memory: Annotated[str, AfterValidator(_memory)] = "2Gi"


class Context(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    base_url: HttpUrl
    api_key: SecretStr
    allowed_hosts: list[str]
    codex: CodexConfig
    resources: Resources = Field(default_factory=Resources)
    turn: AppServerTurnOptions = Field(default_factory=AppServerTurnOptions)
    tools: list[protocol.DynamicToolSpec] = Field(default_factory=list[protocol.DynamicToolSpec])
    review: protocol.ReviewTarget | None = None
