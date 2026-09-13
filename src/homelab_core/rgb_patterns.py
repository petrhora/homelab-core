"""Hardware-independent RGB pattern normalization and rendering."""

from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass
from typing import Any, Sequence

from .devices.interfaces import RgbPixel


@dataclass(frozen=True)
class PatternPixel:
    color: RgbPixel
    brightness: float = 1.0


class Pattern:
    type = "pattern"

    def render(self, led_count: int, elapsed: float) -> tuple[PatternPixel, ...]:
        raise NotImplementedError


@dataclass(frozen=True)
class SolidPattern(Pattern):
    pixels: tuple[PatternPixel, ...]
    type: str = "solid"

    def render(self, led_count: int, elapsed: float) -> tuple[PatternPixel, ...]:
        return self.pixels


@dataclass(frozen=True)
class BreathingPattern(Pattern):
    pixels: tuple[PatternPixel, ...]
    period_ms: int = 2000
    min_intensity: float = 0.0
    max_intensity: float = 1.0
    type: str = "breathing"

    def render(self, led_count: int, elapsed: float) -> tuple[PatternPixel, ...]:
        phase = (elapsed * 1000 / self.period_ms) * 2 * math.pi
        wave = (math.sin(phase - math.pi / 2) + 1) / 2
        intensity = self.min_intensity + wave * (self.max_intensity - self.min_intensity)
        return tuple(PatternPixel(pixel.color, pixel.brightness * intensity) for pixel in self.pixels)


@dataclass(frozen=True)
class FlowPattern(Pattern):
    pixels: tuple[PatternPixel, ...]
    speed: float = 1.0
    direction: str = "forward"
    virtual_led_count: int | None = None
    type: str = "flow"

    def render(self, led_count: int, elapsed: float) -> tuple[PatternPixel, ...]:
        shift = elapsed * self.speed
        if self.direction == "reverse":
            shift = -shift
        virtual_led_count = self.virtual_led_count or led_count
        return tuple(_interpolate_pixels(self.pixels, (index - shift) * len(self.pixels) / virtual_led_count)
                     for index in range(led_count))


@dataclass(frozen=True)
class StepperPattern(Pattern):
    pixels: tuple[PatternPixel, ...]
    step_leds: int = 1
    step_duration_ms: int = 1000
    direction: str = "forward"
    type: str = "stepper"

    def render(self, led_count: int, elapsed: float) -> tuple[PatternPixel, ...]:
        shift = (math.floor(elapsed * 1000 / self.step_duration_ms) * self.step_leds) % led_count
        if self.direction == "reverse":
            shift = -shift
        return tuple(self.pixels[(index - shift) % led_count] for index in range(led_count))


@dataclass(frozen=True)
class GradientPattern(Pattern):
    colors: tuple[RgbPixel, ...]
    speed: float = 1.0
    direction: str = "forward"
    mode: str = "loop"
    virtual_led_count: int | None = None
    type: str = "gradient"

    def render(self, led_count: int, elapsed: float) -> tuple[PatternPixel, ...]:
        shift = elapsed * self.speed
        if self.direction == "reverse":
            shift = -shift
        colors = self.colors if self.mode == "loop" else self.colors + self.colors[-2:0:-1]
        virtual_led_count = self.virtual_led_count or led_count
        return tuple(_interpolate_pixels(tuple(PatternPixel(color) for color in colors),
                                         (index - shift) * len(colors) / virtual_led_count)
                     for index in range(led_count))


@dataclass(frozen=True)
class RainbowPattern(Pattern):
    speed: float = 0.1
    direction: str = "forward"
    saturation: float = 1.0
    brightness: float = 1.0
    virtual_led_count: int | None = None
    type: str = "rainbow"

    def render(self, led_count: int, elapsed: float) -> tuple[PatternPixel, ...]:
        sign = -1 if self.direction == "reverse" else 1
        offset = elapsed * self.speed * sign
        virtual_led_count = self.virtual_led_count or led_count * 4
        return tuple(
            PatternPixel(_hsv_rgb((index / virtual_led_count + offset) % 1, self.saturation, 1.0), self.brightness)
            for index in range(led_count)
        )


@dataclass(frozen=True)
class HueCyclePattern(Pattern):
    speed: float = 0.1
    saturation: float = 1.0
    brightness: float = 1.0
    type: str = "hue_cycle"

    def render(self, led_count: int, elapsed: float) -> tuple[PatternPixel, ...]:
        color = _hsv_rgb(elapsed * self.speed % 1, self.saturation, 1.0)
        return tuple(PatternPixel(color, self.brightness) for _ in range(led_count))


def normalize_pattern(payload: Any, led_count: int, fallback_color: RgbPixel) -> Pattern:
    """Validate API/config data and turn it into a renderer-owned pattern."""
    if payload is None:
        return SolidPattern(tuple(PatternPixel(fallback_color) for _ in range(led_count)))
    if not isinstance(payload, dict):
        raise ValueError("pattern must be an object")

    pattern_type = payload.get("type", "solid")
    if pattern_type == "solid":
        if "pixels" in payload and "color" in payload:
            raise ValueError("solid pattern accepts either color or pixels, not both")
        if "pixels" in payload:
            values = payload["pixels"]
            if not isinstance(values, list) or len(values) != led_count:
                raise ValueError("solid pattern pixels must match led_count")
            pixels = tuple(_parse_pixel(value) for value in values)
        else:
            pixels = tuple(PatternPixel(_parse_color(payload.get("color", _format_color(fallback_color))))
                           for _ in range(led_count))
        return SolidPattern(pixels)
    if pattern_type == "breathing":
        pixels = _source_pixels(payload, led_count, fallback_color)
        period = int(_number(payload, "period_ms", 2000, integer=True, minimum=100))
        low = _number(payload, "min_intensity", 0.0, minimum=0, maximum=1)
        high = _number(payload, "max_intensity", 1.0, minimum=0, maximum=1)
        if low > high:
            raise ValueError("min_intensity must not exceed max_intensity")
        return BreathingPattern(pixels, period, low, high)
    if pattern_type == "flow":
        virtual_led_count = _virtual_led_count(payload, led_count)
        pixels = _source_pixels(payload, led_count, fallback_color, allow_variable_length=virtual_led_count is not None)
        speed = _number(payload, "speed", 1.0, minimum=0)
        direction = payload.get("direction", "forward")
        if direction not in ("forward", "reverse"):
            raise ValueError("direction must be forward or reverse")
        return FlowPattern(pixels, speed, direction, virtual_led_count)
    if pattern_type == "stepper":
        pixels = _source_pixels(payload, led_count, fallback_color)
        step_leds = int(_number(payload, "step_leds", 1, integer=True, minimum=1))
        step_duration_ms = int(_number(payload, "step_duration_ms", 1000, integer=True, minimum=100))
        return StepperPattern(pixels, step_leds, step_duration_ms, _direction(payload))
    if pattern_type == "gradient":
        values = payload.get("colors")
        if not isinstance(values, list) or len(values) < 2:
            raise ValueError("gradient colors must contain at least two colors")
        mode = payload.get("mode", "loop")
        if mode not in ("loop", "ping_pong"):
            raise ValueError("gradient mode must be loop or ping_pong")
        return GradientPattern(tuple(_parse_color(color) for color in values),
                               _number(payload, "speed", 1.0, minimum=0), _direction(payload), mode,
                               _virtual_led_count(payload, led_count))
    if pattern_type == "rainbow":
        virtual_led_count = _virtual_led_count(payload, led_count)
        return RainbowPattern(_number(payload, "speed", 0.1, minimum=0),
                               _direction(payload),
                               _number(payload, "saturation", 1.0, minimum=0, maximum=1),
                               _number(payload, "brightness", 1.0, minimum=0, maximum=1), virtual_led_count)
    if pattern_type == "hue_cycle":
        return HueCyclePattern(_number(payload, "speed", 0.1, minimum=0),
                               _number(payload, "saturation", 1.0, minimum=0, maximum=1),
                               _number(payload, "brightness", 1.0, minimum=0, maximum=1))
    raise ValueError(f"unknown RGB pattern: {pattern_type}")


def adapt_retained_pattern(payload: dict[str, Any], old_led_count: int, new_led_count: int) -> dict[str, Any]:
    """Regenerate uniform retained sources when the strip length changes."""
    if old_led_count == new_led_count or payload.get("type") not in ("solid", "breathing", "flow", "stepper"):
        return payload
    if payload.get("type") == "flow" and payload.get("virtual_led_count") is not None:
        return payload
    pixels = payload.get("pixels")
    if not isinstance(pixels, list) or len(pixels) != old_led_count:
        return payload
    if not pixels or any(pixel != pixels[0] for pixel in pixels[1:]):
        raise ValueError("led_count changes require a replacement pattern for per-LED colors")
    adapted = dict(payload)
    adapted["pixels"] = [pixels[0]] * new_led_count
    return adapted


def apply_brightness(frame: Sequence[PatternPixel], brightness: float) -> list[RgbPixel]:
    return [tuple(round(channel * pixel.brightness * brightness) for channel in pixel.color)  # type: ignore[return-value]
            for pixel in frame]


def pattern_to_payload(pattern: Pattern) -> dict[str, Any]:
    if isinstance(pattern, (SolidPattern, BreathingPattern, FlowPattern, StepperPattern)):
        payload: dict[str, Any] = {
            "type": pattern.type,
            "pixels": [{"color": _format_color(pixel.color), "brightness": pixel.brightness}
                        for pixel in pattern.pixels],
        }
        if isinstance(pattern, BreathingPattern):
            payload.update({"period_ms": pattern.period_ms, "min_intensity": pattern.min_intensity,
                            "max_intensity": pattern.max_intensity})
        elif isinstance(pattern, FlowPattern):
            payload.update({"speed": pattern.speed, "direction": pattern.direction})
            if pattern.virtual_led_count is not None:
                payload["virtual_led_count"] = pattern.virtual_led_count
        elif isinstance(pattern, StepperPattern):
            payload.update({"step_leds": pattern.step_leds, "step_duration_ms": pattern.step_duration_ms,
                            "direction": pattern.direction})
        return payload
    if isinstance(pattern, GradientPattern):
        payload = {"type": pattern.type, "colors": [_format_color(color) for color in pattern.colors],
                   "speed": pattern.speed, "direction": pattern.direction, "mode": pattern.mode}
        if pattern.virtual_led_count is not None:
            payload["virtual_led_count"] = pattern.virtual_led_count
        return payload
    if isinstance(pattern, RainbowPattern):
        payload = {"type": pattern.type, "speed": pattern.speed, "direction": pattern.direction,
                   "saturation": pattern.saturation, "brightness": pattern.brightness}
        if pattern.virtual_led_count is not None:
            payload["virtual_led_count"] = pattern.virtual_led_count
        return payload
    if isinstance(pattern, HueCyclePattern):
        return {"type": pattern.type, "speed": pattern.speed, "saturation": pattern.saturation,
                "brightness": pattern.brightness}
    raise ValueError(f"unsupported RGB pattern: {type(pattern).__name__}")


def _source_pixels(payload: dict[str, Any], led_count: int, fallback_color: RgbPixel,
                   *, allow_variable_length: bool = False) -> tuple[PatternPixel, ...]:
    if "pixels" in payload:
        values = payload["pixels"]
        if (not isinstance(values, list) or not values
                or not allow_variable_length and len(values) != led_count):
            raise ValueError("pattern pixels must match led_count")
        return tuple(_parse_pixel(value) for value in values)
    color = _parse_color(payload.get("color", _format_color(fallback_color)))
    return tuple(PatternPixel(color) for _ in range(led_count))


def _parse_pixel(value: Any) -> PatternPixel:
    if isinstance(value, str):
        return PatternPixel(_parse_color(value))
    if isinstance(value, dict):
        return PatternPixel(_parse_color(value.get("color")), _number(value, "brightness", 1.0, minimum=0, maximum=1))
    raise ValueError("pixels must contain colors or {color, brightness} objects")


def _interpolate_pixels(pixels: Sequence[PatternPixel], position: float) -> PatternPixel:
    lower = math.floor(position)
    fraction = position - lower
    start = pixels[lower % len(pixels)]
    end = pixels[(lower + 1) % len(pixels)]
    return PatternPixel((
        round(start.color[0] + (end.color[0] - start.color[0]) * fraction),
        round(start.color[1] + (end.color[1] - start.color[1]) * fraction),
        round(start.color[2] + (end.color[2] - start.color[2]) * fraction),
    ),
                        start.brightness + (end.brightness - start.brightness) * fraction)  # type: ignore[arg-type]


def _virtual_led_count(payload: dict[str, Any], led_count: int) -> int | None:
    value = payload.get("virtual_led_count")
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < led_count):
        raise ValueError("virtual_led_count must be an integer no smaller than led_count")
    return value


def _parse_color(value: Any) -> RgbPixel:
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        raise ValueError("color must be in #RRGGBB format")
    try:
        return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]
    except ValueError:
        raise ValueError("color must be in #RRGGBB format") from None


def _number(payload: dict[str, Any], name: str, default: float, *, integer: bool = False,
            minimum: float | None = None, maximum: float | None = None) -> float | int:
    value = payload.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} is out of range")
    if integer and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"{name} must be an integer")
    return value


def _direction(payload: dict[str, Any]) -> str:
    direction = payload.get("direction", "forward")
    if direction not in ("forward", "reverse"):
        raise ValueError("direction must be forward or reverse")
    return direction


def _format_color(color: RgbPixel) -> str:
    return "#%02X%02X%02X" % color


def _hsv_rgb(hue: float, saturation: float, value: float) -> RgbPixel:
    red, green, blue = colorsys.hsv_to_rgb(hue, saturation, value)
    return round(red * 255), round(green * 255), round(blue * 255)
