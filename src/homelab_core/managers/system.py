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
            self._snapshot = self._collect()
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
                self._snapshot = self._collect()
                await self.state.update(self.name, self._snapshot)
            except Exception as error:
                self._error = str(error)
                self._status = ManagerStatus.DEGRADED

    def _collect(self) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        disks = []
        for disk in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(disk.mountpoint)
            except OSError:
                continue
            disks.append({"device": disk.device, "mountpoint": disk.mountpoint, "fstype": disk.fstype,
                          "total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free,
                          "used_ratio": usage.percent / 100})
        interfaces = {}
        for name, addresses in psutil.net_if_addrs().items():
            interfaces[name] = [{"family": str(address.family), "address": address.address,
                                 "netmask": address.netmask, "mac": address.address if address.family == psutil.AF_LINK else None}
                                for address in addresses]
        return {"collected_at": utc_now(), "hostname": platform.node(), "os": platform.platform(),
                "kernel": platform.release(), "architecture": platform.machine(), "cpu_model": platform.processor(),
                "cpu_cores": psutil.cpu_count(logical=True), "cpu_usage_ratio": psutil.cpu_percent() / 100,
                "cpu_per_core_usage_ratio": [value / 100 for value in psutil.cpu_percent(percpu=True)],
                "cpu_temperature_celsius": self._temperature(), "load_average": list(psutil.getloadavg()),
                "uptime_seconds": time.time() - psutil.boot_time(), "memory": {"total_bytes": memory.total,
                "used_bytes": memory.used, "available_bytes": memory.available, "used_ratio": memory.percent / 100},
                "storage": disks, "network_interfaces": interfaces,
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

    async def processes(self) -> list[dict[str, Any]]:
        processes = []
        for process in psutil.process_iter(["pid", "name", "cmdline", "status", "username"]):
            try:
                info = process.info
                processes.append({**info, "cmdline": info.get("cmdline") or []})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return processes
