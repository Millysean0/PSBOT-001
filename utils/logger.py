"""Modul logging terpusat untuk PSBOT-001.

Fitur:
  - Output berwarna di terminal (colorama) untuk kemudahan debug
  - File log bot.log tanpa warna (plain text)
  - Format ringkas di konsol, format lengkap di file
  - Startup banner saat bot pertama kali diinisialisasi
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

try:
    import colorama
    from colorama import Fore, Style
    colorama.init(autoreset=True)
    _HAS_COLOR = True
except ImportError:
    _HAS_COLOR = False


# ─── Level → (warna, label pendek) ──────────────────────────────────────────
_LEVEL_STYLES: dict[int, tuple[str, str]] = {
    logging.DEBUG:    (Fore.CYAN    if _HAS_COLOR else "", "DBG"),
    logging.INFO:     (Fore.GREEN   if _HAS_COLOR else "", "INF"),
    logging.WARNING:  (Fore.YELLOW  if _HAS_COLOR else "", "WRN"),
    logging.ERROR:    (Fore.RED     if _HAS_COLOR else "", "ERR"),
    logging.CRITICAL: (Fore.MAGENTA if _HAS_COLOR else "", "CRT"),
}

_LEVEL_ICONS: dict[int, str] = {
    logging.DEBUG:    "🔍",
    logging.INFO:     "✅",
    logging.WARNING:  "⚠️ ",
    logging.ERROR:    "❌",
    logging.CRITICAL: "🔥",
}


class _ColorConsoleFormatter(logging.Formatter):
    """Formatter konsol berwarna & ringkas — tampil di terminal CMD/PowerShell."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        color, label = _LEVEL_STYLES.get(record.levelno, ("", "???"))
        icon = _LEVEL_ICONS.get(record.levelno, " ")
        reset = Style.RESET_ALL if _HAS_COLOR else ""

        # Nama modul — potong prefix duplikat (psbot / core / handlers)
        mod = record.name
        for prefix in ("psbot.", "core.", "handlers.", "utils."):
            if mod.startswith(prefix):
                mod = mod[len(prefix):]
                break

        time_str = self.formatTime(record, "%H:%M:%S")
        header = f"{color}{time_str} {icon} [{label}]{reset}"
        module_tag = f"{Fore.CYAN if _HAS_COLOR else ''}[{mod}]{reset}"
        msg = record.getMessage()

        line = f"{header} {module_tag} {msg}"

        if record.exc_info:
            # Tambahkan traceback di bawah, indent 4 spasi
            exc_text = self.formatException(record.exc_info)
            indented = "\n".join("    " + l for l in exc_text.splitlines())
            line = f"{line}\n{Fore.RED if _HAS_COLOR else ''}{indented}{reset}"

        return line


class _PlainFileFormatter(logging.Formatter):
    """Formatter file — format lengkap tanpa warna ANSI."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        return super().format(record)


def print_startup_banner(bot_name: str = "PSBOT-001", version: str = "2.0") -> None:
    """Mencetak banner startup ke konsol saat bot pertama kali jalan."""
    if _HAS_COLOR:
        border = Fore.CYAN + Style.BRIGHT + "═" * 52 + Style.RESET_ALL
        title = Fore.GREEN + Style.BRIGHT + f"  🤖  {bot_name}  v{version}" + Style.RESET_ALL
        sub   = Fore.YELLOW + "  Telegram Bot · ETHOL · SKEM · WhatsApp" + Style.RESET_ALL
    else:
        border = "═" * 52
        title = f"  🤖  {bot_name}  v{version}"
        sub   = "  Telegram Bot · ETHOL · SKEM · WhatsApp"

    print(border)
    print(title)
    print(sub)
    print(border)
    print()


def setup_logger(
    name: str = "psbot",
    log_dir: str = "logs",
    level: str | int = logging.INFO,
) -> logging.Logger:
    """Mengatur dan mengembalikan logger dengan handler stream (berwarna) dan file (plain).

    Args:
        name:    Nama logger (biasanya __name__ atau 'psbot').
        log_dir: Direktori tempat file bot.log disimpan.
        level:   Level logging ('DEBUG', 'INFO', 'WARNING', 'ERROR', atau konstanta int).

    Returns:
        Instance logging.Logger yang sudah dikonfigurasi.
    """
    if isinstance(level, str):
        numeric_level = getattr(logging, level.strip().upper(), logging.INFO)
    elif isinstance(level, int):
        numeric_level = level
    else:
        numeric_level = logging.INFO

    logger = logging.getLogger(name)
    logger.setLevel(numeric_level)

    # Hindari duplikasi handler jika setup_logger dipanggil ulang
    if logger.handlers:
        for handler in logger.handlers:
            handler.setLevel(numeric_level)
        return logger

    # ── File handler (plain text, format lengkap) ─────────────────────────
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(log_path / "bot.log", encoding="utf-8")
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(
        _PlainFileFormatter(
            fmt="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # ── Console handler (berwarna, ringkas) ───────────────────────────────
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(numeric_level)
    stream_handler.setFormatter(_ColorConsoleFormatter(datefmt="%H:%M:%S"))

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger
