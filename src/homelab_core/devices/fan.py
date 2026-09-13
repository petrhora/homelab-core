import asyncio

from .interfaces import FanDevice


class GpioFanDevice(FanDevice):
    """GPIO6 fan and GPIO5 RGB-enable adapter for Pironman-style hardware."""

    def __init__(self, fan_pin: int, led_pin: int, *, fan_active_high: bool, led_active_high: bool) -> None:
        self.fan_pin = fan_pin
        self.led_pin = led_pin
        self.fan_active_high = fan_active_high
        self.led_active_high = led_active_high
        self._gpio = None

    async def set_fan(self, enabled: bool) -> None:
        await asyncio.to_thread(self._set_output, self.fan_pin, enabled, self.fan_active_high)

    async def set_led(self, enabled: bool) -> None:
        await asyncio.to_thread(self._set_output, self.led_pin, enabled, self.led_active_high)

    async def close(self) -> None:
        if self._gpio is not None:
            await asyncio.to_thread(self._gpio.cleanup, (self.fan_pin, self.led_pin))
            self._gpio = None

    def _set_output(self, pin: int, enabled: bool, active_high: bool) -> None:
        if self._gpio is None:
            try:
                from RPi import GPIO
            except ImportError as error:
                raise RuntimeError("GPIO support requires the rpi-lgpio package") from error
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.fan_pin, GPIO.OUT,
                       initial=GPIO.LOW if self.fan_active_high else GPIO.HIGH)
            GPIO.setup(self.led_pin, GPIO.OUT,
                       initial=GPIO.LOW if self.led_active_high else GPIO.HIGH)
            self._gpio = GPIO
        self._gpio.output(pin, enabled if active_high else not enabled)
