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


class ExternalFanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pin: int = Field(default=6, ge=0, le=53)
    active_high: bool = True
    mode: Literal["off", "on", "auto"] = "auto"
    on_temperature_c: float = Field(default=55.0, ge=0, le=120)
    off_temperature_c: float = Field(default=50.0, ge=0, le=120)


class FanLedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pin: int = Field(default=5, ge=0, le=53)
    active_high: bool = True
    mode: Literal["off", "on", "follow"] = "on"


class FansConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    poll_interval_seconds: float = Field(default=1.0, gt=0, le=3600)
    temperature_path: Path = Path("/sys/class/thermal/thermal_zone0/temp")
    external: ExternalFanConfig = ExternalFanConfig()
    led: FanLedConfig = FanLedConfig()

    def model_post_init(self, __context: object) -> None:
        if self.external.off_temperature_c > self.external.on_temperature_c:
            raise ValueError("fans.external.off_temperature_c must not exceed on_temperature_c")


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api: ApiConfig = ApiConfig()
    logging: LoggingConfig = LoggingConfig()
    system: SystemConfig = SystemConfig()
    rgb: RgbConfig = RgbConfig()
    fans: FansConfig = FansConfig()
    metrics_enabled: bool = True


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or Path(os.environ.get("HOMELAB_CORE_CONFIG", "/etc/homelab-core/config.toml"))
    if not config_path.exists():
        return AppConfig()
    with config_path.open("rb") as config_file:
        return AppConfig.model_validate(tomllib.load(config_file))
