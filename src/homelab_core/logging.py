import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LoggingConfig


def configure_logging(config: LoggingConfig) -> None:
    level = getattr(logging, config.level.upper(), None)
    if not isinstance(level, int):
        raise ValueError(f"Unknown log level: {config.level}")
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        Path(config.file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(config.file, maxBytes=config.max_bytes, backupCount=config.backups))
    except OSError:
        logging.getLogger(__name__).warning("File logging unavailable: %s", config.file)
    for handler in handlers:
        handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    for handler in handlers:
        root.addHandler(handler)
