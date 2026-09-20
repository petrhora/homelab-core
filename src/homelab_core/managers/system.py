import asyncio
import platform
import time
from typing import Any

import psutil

from ..config import SystemConfig
from ..runtime.state import StateStore, utc_now
from .base import Manager, ManagerStatus


class SystemManager(Manager):
    name = "system"

    def __init__(self, config: SystemConfig, state: StateStore) -> None:
        self.config = config
        self.state = state
        self._status = ManagerStatus.STARTING
        self._task: asyncio.Task | None = None
        self._snapshot: dict[str, Any] = {}
        self._error: str | None = None

    async def start(self) -> None:
        try:
            self._snapshot = await asyncio.to_thread(self._collect)
            await self.state.update(self.name, self._snapshot)
            self._status = ManagerStatus.READY
            self._task = asyncio.create_task(self._poll(), name="system-poll")
        except Exception as error:
            self._error = str(error)
            self._status = ManagerStatus.DEGRADED

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._status = ManagerStatus.STOPPED

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.config.poll_interval_seconds)
            try:
                self._snapshot = await asyncio.to_thread(self._collect)
                await self.state.update(self.name, self._snapshot)
                self._error = None
                self._status = ManagerStatus.READY
            except Exception as error:
                self._error = str(error)
                self._status = ManagerStatus.DEGRADED

    def _collect(self) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        cpu_frequency = psutil.cpu_freq()
        cpu_times = psutil.cpu_times()
        cpu_times_per_core = psutil.cpu_times(percpu=True)

        def cpu_mode_ratios(times: Any) -> dict[str, float]:
            values = [getattr(times, name, 0.0) for name in ("user", "system", "idle")]
            total = sum(getattr(times, field, 0.0) for field in times._fields)
            return {name: value / total if total else 0.0 for name, value in zip(("user", "system", "idle"), values)}

        disks = []
        for disk in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(disk.mountpoint)
            except OSError:
                continue
            disks.append({"device": disk.device, "mountpoint": disk.mountpoint, "fstype": disk.fstype,
                          "total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free,
                          "used_ratio": usage.percent / 100})
        disk_io = {name: {"read_bytes": value.read_bytes, "write_bytes": value.write_bytes}
                   for name, value in psutil.disk_io_counters(perdisk=True).items()}
        interfaces = {}
        for name, addresses in psutil.net_if_addrs().items():
            interfaces[name] = [{"family": str(address.family), "address": address.address,
                                 "netmask": address.netmask, "mac": address.address if address.family == psutil.AF_LINK else None}
                                for address in addresses]
        return {"collected_at": utc_now(), "hostname": platform.node(), "os": platform.platform(),
                "kernel": platform.release(), "architecture": platform.machine(), "cpu_model": platform.processor(),
                "cpu_cores": psutil.cpu_count(logical=True), "cpu_usage_ratio": psutil.cpu_percent() / 100,
                 "cpu_per_core_usage_ratio": [value / 100 for value in psutil.cpu_percent(percpu=True)],
                 "cpu_usage_ratio_by_mode": {"total": cpu_mode_ratios(cpu_times),
                                              **{str(index): cpu_mode_ratios(times)
                                                 for index, times in enumerate(cpu_times_per_core)}},
                 "cpu_frequency_hertz": cpu_frequency.current * 1_000_000 if cpu_frequency is not None else None,
                 "cpu_temperature_celsius": self._temperature(), "load_average": list(psutil.getloadavg()),
                 "uptime_seconds": time.time() - psutil.boot_time(), "memory": {"total_bytes": memory.total,
                 "used_bytes": memory.used, "available_bytes": memory.available, "cached_bytes": memory.cached,
                 "used_ratio": memory.percent / 100},
                 "swap": {"used_bytes": swap.used, "total_bytes": swap.total},
                 "storage": disks, "disk_io": disk_io, "network_interfaces": interfaces,
                "network_io": {name: {"receive_bytes": value.bytes_recv, "transmit_bytes": value.bytes_sent}
                               for name, value in psutil.net_io_counters(pernic=True).items()}}

    @staticmethod
    def _temperature() -> float | None:
        sensors_temperatures = getattr(psutil, "sensors_temperatures", None)
        if sensors_temperatures is None:
            return None
        for entries in sensors_temperatures().values():
            if entries and entries[0].current is not None:
                return entries[0].current
        return None

    async def health(self) -> dict:
        return {"status": self._status.value, "error": self._error}

    async def snapshot(self) -> dict:
        return self._snapshot

    async def processes(self, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        processes = []
        has_more = False
        skipped = 0
        for process in psutil.process_iter(["pid", "name", "status", "username"]):
            try:
                info = process.info
                if skipped < offset:
                    skipped += 1
                    continue
                if len(processes) >= limit:
                    has_more = True
                    break
                processes.append({key: info.get(key) for key in ("pid", "name", "status", "username")})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {"items": processes, "offset": offset, "limit": limit, "has_more": has_more}
