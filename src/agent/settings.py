import os
from datetime import timedelta
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr


def _docker(path: str) -> str:
    windows = PureWindowsPath(path)
    if not windows.drive.endswith(":"):
        return path
    return PurePosixPath("/run/desktop/mnt/host", windows.drive[0].lower(), *windows.parts[1:]).as_posix()


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, hide_input_in_errors=True)

    token: SecretStr = Field(validation_alias="API_KEY", min_length=32)
    domain: str = Field(validation_alias="OPEN_SANDBOX_DOMAIN")
    key: SecretStr = Field(validation_alias="OPEN_SANDBOX_API_KEY")
    mount: Annotated[str, AfterValidator(_docker)] = Field(validation_alias="SANDBOX_MOUNT_PATH")
    idle: timedelta = Field(validation_alias="SANDBOX_IDLE")
    retention: timedelta = Field(validation_alias="SANDBOX_RETENTION")


settings = Settings.model_validate(dict(os.environ))
