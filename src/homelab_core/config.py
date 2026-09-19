from pathlib import Path
import os
import tomllib
from datetime import time
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


RgbColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]
TimeOfDay = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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


class OledConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    bus: int = Field(default=1, ge=0, le=31)
    address: Literal["auto", "0x3c", "0x3d"] = "auto"
    refresh_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    sleep_timeout_seconds: float = Field(default=15, ge=0, le=3600)
    rotation: Literal[0, 180] = 0


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


class LightingWindowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: TimeOfDay
    end: TimeOfDay
    days: list[Weekday] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    @model_validator(mode="after")
    def validate_window(self) -> "LightingWindowConfig":
        if self.start == self.end:
            raise ValueError("lighting window start and end must differ")
        if not self.days:
            raise ValueError("lighting window days must not be empty")
        if len(set(self.days)) != len(self.days):
            raise ValueError("lighting window days must not contain duplicates")
        return self


class LightingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    timezone: str | None = None
    windows: list[LightingWindowConfig] = []

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("lighting.timezone must be a valid IANA timezone") from None
        return value


class PowerButtonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    device_name: str = "pwr_button"
    device_path: str | None = None
    debounce_ms: int = Field(default=50, ge=0, le=1000)
    double_click_window_ms: int = Field(default=400, ge=100, le=5000)
    long_press_ms: int = Field(default=3000, ge=500, le=60_000)
    long_press_action: Literal["none", "shutdown", "reboot"] = "shutdown"


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api: ApiConfig = ApiConfig()
    logging: LoggingConfig = LoggingConfig()
    system: SystemConfig = SystemConfig()
    rgb: RgbConfig = RgbConfig()
    oled: OledConfig = OledConfig()
    fans: FansConfig = FansConfig()
    lighting: LightingConfig = LightingConfig()
    power_button: PowerButtonConfig = PowerButtonConfig()
    metrics_enabled: bool = True
    persistence_path: Path = Path("/var/lib/homelab-core/state.json")


def load_config(path: Path | None = None) -> AppConfig:
    config_path = path or Path(os.environ.get("HOMELAB_CORE_CONFIG", "/etc/homelab-core/config.toml"))
    if not config_path.exists():
        return AppConfig()
    with config_path.open("rb") as config_file:
        return AppConfig.model_validate(tomllib.load(config_file))
