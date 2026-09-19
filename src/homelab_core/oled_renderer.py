import ipaddress
from typing import Any, Mapping


_FONT = {
    " ": (0, 0, 0, 0, 0), "%": (17, 2, 4, 8, 17), "-": (8, 8, 8, 8, 8), ".": (0, 0, 0, 0, 16), ":": (0, 20, 0, 20, 0),
    "0": (14, 17, 19, 21, 14), "1": (4, 12, 4, 4, 14), "2": (14, 17, 2, 4, 31),
    "3": (30, 1, 6, 1, 30), "4": (2, 6, 10, 31, 2), "5": (31, 16, 30, 1, 30),
    "6": (6, 8, 30, 17, 14), "7": (31, 1, 2, 4, 4), "8": (14, 17, 14, 17, 14),
    "9": (14, 17, 15, 1, 6),
}
for _letter, _rows in {
    "A": (14, 17, 31, 17, 17), "B": (30, 17, 30, 17, 30), "C": (14, 17, 16, 17, 14), "D": (30, 17, 17, 17, 30),
    "E": (31, 16, 30, 16, 31), "F": (31, 16, 30, 16, 16), "G": (14, 17, 23, 1, 30),
    "H": (17, 17, 31, 17, 17), "I": (14, 4, 4, 4, 14), "J": (1, 1, 1, 17, 14), "K": (17, 18, 28, 18, 17), "M": (17, 27, 21, 17, 17), "N": (17, 25, 21, 19, 17),
    "O": (14, 17, 17, 17, 14), "P": (30, 17, 30, 16, 16), "R": (30, 17, 30, 20, 18),
    "Q": (14, 17, 17, 19, 15), "S": (15, 16, 14, 1, 30), "T": (31, 4, 4, 4, 4), "U": (17, 17, 17, 17, 14),
    "V": (17, 17, 17, 10, 4), "W": (17, 17, 21, 27, 17), "X": (17, 10, 4, 10, 17), "Y": (17, 17, 10, 4, 4), "Z": (31, 2, 4, 8, 31), "L": (16, 16, 16, 16, 31),
}.items():
    _FONT[_letter] = _rows


def _put_pixel(frame: bytearray, x: int, y: int, width: int, height: int) -> None:
    if 0 <= x < width and 0 <= y < height:
        frame[x + (y // 8) * width] |= 1 << (y % 8)


def _text(frame: bytearray, text: str, x: int, y: int, width: int, height: int) -> None:
    for character in text.upper():
        glyph = _FONT.get(character, _FONT[" "])
        for source_x, bits in enumerate(glyph):
            for source_y in range(5):
                if bits & (1 << source_y):
                    _put_pixel(frame, x + 4 - source_y, y + source_x, width, height)
        x += 6


def _root_disk_usage(storage: Any) -> float:
    if not isinstance(storage, list):
        return 0
    for disk in storage:
        if isinstance(disk, Mapping) and disk.get("mountpoint") == "/":
            return float(disk.get("used_ratio", 0))
    return 0


def _primary_ipv4(interfaces: Any) -> str:
    if not isinstance(interfaces, Mapping):
        return "--"
    for addresses in interfaces.values():
        if not isinstance(addresses, list):
            continue
        for address in addresses:
            if not isinstance(address, Mapping):
                continue
            try:
                candidate = ipaddress.ip_address(str(address.get("address", "")))
            except ValueError:
                continue
            if candidate.version == 4 and not candidate.is_loopback:
                return str(candidate)
    return "--"


def render_status(snapshot: Mapping[str, Any], width: int = 128, height: int = 64, rotation: int = 0) -> bytes:
    if (width, height) != (128, 64):
        raise ValueError("Only 128x64 OLEDs are supported")
    memory = snapshot.get("memory", {})
    temperature = snapshot.get("cpu_temperature_celsius")
    temp_text = "--" if temperature is None else f"{temperature:.0f}C"
    ip_address = _primary_ipv4(snapshot.get("network_interfaces"))
    hostname = str(snapshot.get("hostname", ""))
    hostname = hostname[:max(0, 21 - len(ip_address) - 3)]
    lines = (
        f"CPU {snapshot.get('cpu_usage_ratio', 0) * 100:3.0f}% RAM {memory.get('used_ratio', 0) * 100:3.0f}%",
        f"DSK {_root_disk_usage(snapshot.get('storage')) * 100:3.0f}% TMP {temp_text}",
        "",
        f"{hostname} - {ip_address}",
    )
    frame = bytearray(width * height // 8)
    for index, line in enumerate(lines):
        _text(frame, line, 0, index * 16, width, height)
    if rotation == 180:
        rotated = bytearray(len(frame))
        for y in range(height):
            for x in range(width):
                source = x + (y // 8) * width
                if frame[source] & (1 << (y % 8)):
                    _put_pixel(rotated, width - 1 - x, height - 1 - y, width, height)
        frame = rotated
    return bytes(frame)


def render_shutdown() -> bytes:
    frame = bytearray(128 * 64 // 8)
    _text(frame, "SHUTDOWN", 40, 28, 128, 64)
    return bytes(frame)
