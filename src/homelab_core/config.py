from pathlib import Path
import os
import tomllib

from pydantic import BaseModel, ConfigDict, Field


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: str = "INFO"
    file: Path = Path("/var/log/homelab-core/homelab-core.log")
    max_bytes: int = Field(default=10_000_000, ge=1_000)
    backups: int = Field(default=5, ge=0, le=100)


class SystemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    poll_interval_seconds: float = Field(default=5.0, gt=0, le=3600)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api: ApiConfig = ApiConfig()
    logging: LoggingConfig = LoggingConfig()
    system: SystemConfig = SystemConfig()
    metrics_enabled: bool = True


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or Path(os.environ.get("HOMELAB_CORE_CONFIG", "/etc/homelab-core/config.toml"))
    if not config_path.exists():
        return AppConfig()
    with config_path.open("rb") as config_file:
        return AppConfig.model_validate(tomllib.load(config_file))
