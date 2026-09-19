from __future__ import annotations

import asyncio
from typing import Protocol

SYSTEMCTL_TIMEOUT_SECONDS = 30
PROCESS_CLEANUP_TIMEOUT_SECONDS = 5


class SystemActions(Protocol):
    async def shutdown(self) -> None: ...

    async def reboot(self) -> None: ...


class SystemctlActions:
    async def shutdown(self) -> None:
        await self._run("poweroff")

    async def reboot(self) -> None:
        await self._run("reboot")

    @staticmethod
    async def _run(action: str) -> None:
        process = await asyncio.create_subprocess_exec(
            "systemctl", action, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        completion = asyncio.create_task(process.communicate())
        try:
            _, stderr = await asyncio.wait_for(asyncio.shield(completion), SYSTEMCTL_TIMEOUT_SECONDS)
        except (asyncio.CancelledError, TimeoutError):
            await SystemctlActions._terminate(process, completion)
            raise
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            message = f"systemctl {action} failed with exit code {process.returncode}"
            raise RuntimeError(f"{message}: {detail}" if detail else message)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process, completion: asyncio.Task[tuple[bytes, bytes]]) -> None:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(asyncio.shield(completion), PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except TimeoutError:
            if process.returncode is None:
                process.kill()
            await asyncio.shield(completion)
