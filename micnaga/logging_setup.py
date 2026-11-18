"""Log rotatorio a fichero en la carpeta de logs de la app. Sin red, sin consola por defecto.

Llamar explicitamente desde el ``run`` de cada proceso (``setup_logging("worker")``);
importar el modulo no crea ficheros ni carpetas.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from micnaga import paths

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 5
_MARK = "_micnaga_file_handler"


def setup_logging(
    process: str = "micnaga",
    *,
    level: int = logging.INFO,
    console: bool = False,
    directory: Path | None = None,
) -> Path:
    """Configura el logger raiz ``micnaga`` y devuelve la ruta del fichero (``<process>.log``).

    Idempotente: si ya hay un manejador de fichero para ese proceso no anade otro.
    """
    log_dir = paths.ensure_dir(directory or paths.logs_dir())
    log_file = log_dir / f"{process}.log"
    logger = logging.getLogger("micnaga")
    logger.setLevel(level)
    logger.propagate = False
    for handler in logger.handlers:
        if getattr(handler, _MARK, None) == str(log_file):
            return log_file
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    setattr(file_handler, _MARK, str(log_file))
    logger.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(stream)
    return log_file


def shutdown_logging() -> None:
    """Cierra y retira los manejadores de ``micnaga`` (los tests lo usan para borrar ficheros)."""
    logger = logging.getLogger("micnaga")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
