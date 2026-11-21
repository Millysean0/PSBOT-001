"""Modul logging terpusat untuk PSBOT-001."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def setup_logger(
    name: str = "psbot",
    log_dir: str = "logs",
    level: str | int = logging.INFO,
) -> logging.Logger:
    """Mengatur dan mengembalikan logger dengan handler stream dan file."""
    if isinstance(level, str):
        numeric_level = getattr(logging, level.strip().upper(), logging.INFO)
    elif isinstance(level, int):
        numeric_level = level
    else:
        numeric_level = logging.INFO

    logger = logging.getLogger(name)
    logger.setLevel(numeric_level)
    if logger.handlers:
        for handler in logger.handlers:
            handler.setLevel(numeric_level)
        return logger

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path / "bot.log", encoding="utf-8")
    file_handler.setLevel(numeric_level)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(numeric_level)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger
