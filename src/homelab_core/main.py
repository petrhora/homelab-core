import asyncio
import signal

import uvicorn

from .api.server import create_app
from .config import load_config
from .logging import configure_logging
from .runtime.supervisor import Supervisor


async def serve() -> None:
    config = load_config()
    configure_logging(config.logging)
    supervisor = Supervisor(config)
    app = create_app(supervisor, config.metrics_enabled)
    server = uvicorn.Server(uvicorn.Config(app, host=config.api.host, port=config.api.port, log_config=None))
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, lambda: setattr(server, "should_exit", True))
        except NotImplementedError:
            pass
    await supervisor.start()
    try:
        await server.serve()
    finally:
        await supervisor.stop()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
