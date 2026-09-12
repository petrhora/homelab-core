from pathlib import Path
import os
import tomllib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


RgbColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]


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


class RgbConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    device: Literal["ws2812"] = "ws2812"
    spi_path: str = "/dev/spidev0.0"
    led_count: int = Field(default=4, ge=1, le=1024)
    brightness: float = Field(default=1.0, ge=0, le=1)
    color: RgbColor = "#FFFFFF"
    spi_speed_hz: int = Field(default=2_400_000, ge=100_000, le=20_000_000)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api: ApiConfig = ApiConfig()
    logging: LoggingConfig = LoggingConfig()
    system: SystemConfig = SystemConfig()
    rgb: RgbConfig = RgbConfig()
    metrics_enabled: bool = True


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or Path(os.environ.get("HOMELAB_CORE_CONFIG", "/etc/homelab-core/config.toml"))
    if not config_path.exists():
        return AppConfig()
    with config_path.open("rb") as config_file:
        return AppConfig.model_validate(tomllib.load(config_file))
