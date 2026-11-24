"""Titik masuk utama (Entrypoint) untuk PSBOT-001 (PENS Smart Academic Assistant Bot).

Menginisialisasi konfigurasi, logging, validasi environment (.env & config.json),
database SQLite multi-user, Telegram Bot API (python-telegram-bot v21+),
background scheduler (APScheduler), dan Baileys WhatsApp integration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any, Optional

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

from core.database import DatabaseManager
from core.discord_bot import DiscordManager
from core.ethol_client import EtholClient
from core.filter_engine import FilterEngine
from core.notifier import Notifier
from core.scheduler import BotScheduler
from core.whatsapp import WhatsAppManager
from handlers.callbacks import handle_callback_query
from handlers.commands import (
    cmd_absen,
    cmd_connect,
    cmd_deadline,
    cmd_debug,
    cmd_detail,
    cmd_disconnect,
    cmd_filter,
    cmd_login,
    cmd_logout,
    cmd_materi,
    cmd_matkul,
    cmd_mute,
    cmd_pause,
    cmd_resume,
    cmd_skem,
    cmd_start,
    cmd_status,
    cmd_tugas,
    cmd_unconnect,
    cmd_unmute,
)
from utils.crypto import get_fernet
from utils.logger import print_startup_banner, setup_logger


def load_bot_config(config_path: str = "config.json") -> dict:
    """Membaca konfigurasi bot dari config.json."""
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            print(f"Peringatan: Gagal membaca {config_path}: {exc}", file=sys.stderr)
    return {}


def validate_environment() -> tuple[str, str, Optional[int]]:
    """Memvalidasi konfigurasi variabel environment.

    Returns:
        tuple[str, str, Optional[int]]: (bot_token, fernet_key, admin_user_id).
    """
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    fernet_key = os.getenv("FERNET_KEY", "").strip()
    raw_admin_id = os.getenv("ADMIN_USER_ID") or os.getenv("ALLOWED_USER_ID", "").strip()

    errors = []
    if not bot_token or "YOUR_TELEGRAM_BOT_TOKEN" in bot_token:
        errors.append("- BOT_TOKEN belum diset di .env! Dapatkan token dari @BotFather di Telegram.")

    if not fernet_key or "YOUR_GENERATED_FERNET_KEY_HERE" in fernet_key:
        errors.append(
            "- FERNET_KEY belum diset di .env! Jalankan 'python -m utils.crypto' untuk membuat kunci baru."
        )
    else:
        try:
            get_fernet(fernet_key)
        except Exception as exc:
            errors.append(f"- Format FERNET_KEY tidak valid: {exc}")

    admin_user_id: Optional[int] = None
    if raw_admin_id:
        try:
            admin_user_id = int(raw_admin_id)
        except ValueError:
            pass

    if errors:
        print("\n" + "=" * 65, file=sys.stderr)
        print("❌ KESALAHAN KONFIGURASI ENVIRONMENT (.env):", file=sys.stderr)
        print("=" * 65, file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        print("\nSalin .env.example menjadi .env lalu isi nilainya dengan benar.", file=sys.stderr)
        print("=" * 65 + "\n", file=sys.stderr)
        sys.exit(1)

    return bot_token, fernet_key, admin_user_id


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler untuk mencatat exception."""
    logger = setup_logger()
    err_str = str(context.error) if context.error else ""
    if "Conflict" in err_str:
        logger.warning("Konflik Telegram: Ada proses bot lain yang sedang berjalan menggunakan token yang sama: %s", context.error)
        return
    if any(ign in err_str for ign in ("Query is too old", "query id is invalid", "Message is not modified", "Message to edit not found")):
        logger.debug("Telegram non-fatal update event: %s", context.error)
        return
    if any(net_err in err_str or net_err in type(context.error).__name__ for net_err in ("ConnectError", "ConnectTimeout", "ReadTimeout", "NetworkError", "TimedOut", "RemoteProtocolError")):
        err_detail = str(context.error) or type(context.error).__name__
        logger.warning("Koneksi jaringan ke server Telegram mengalami fluktuasi sementara: %s", err_detail)
        return
    logger.error("Exception update tidak tertangani: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ <b>Terjadi kendala pada sistem bot.</b>\n"
                "Silakan coba beberapa saat lagi.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


async def post_init(application: Application) -> None:
    """Callback setelah inisialisasi aplikasi Telegram selesai."""
    logger = setup_logger()
    scheduler: BotScheduler = application.bot_data["scheduler"]

    # Daftarkan daftar perintah bot ke Telegram API
    bot_commands = [
        BotCommand("start", "Mulai bot & buka menu utama"),
        BotCommand("login", "Login akun PENS CAS SSO"),
        BotCommand("connect", "Tautkan akun ke WhatsApp (-connect)"),
        BotCommand("disconnect", "Putus tautan akun dari WhatsApp"),
        BotCommand("matkul", "Daftar mata kuliah semester aktif"),
        BotCommand("detail", "Detail matkul (Jadwal, RPS, Presensi)"),
        BotCommand("tugas", "Daftar tugas aktif"),
        BotCommand("deadline", "Tugas dengan deadline mendesak (≤3 hari)"),
        BotCommand("absen", "Presensi & Auto-Absen mata kuliah"),
        BotCommand("materi", "Daftar materi kuliah terbaru"),
        BotCommand("status", "Status akun & bot"),
        BotCommand("filter", "Filter blacklist & watchlist MK"),
        BotCommand("mute", "Mute notifikasi kategori"),
        BotCommand("unmute", "Unmute kategori notifikasi"),
        BotCommand("pause", "Jeda background polling"),
        BotCommand("resume", "Lanjutkan background polling"),
        BotCommand("skem", "Pantau SKEM (/skem <nrp>) / Cek (/skem check)"),
        BotCommand("logout", "Logout & hapus kredensial"),
    ]
    try:
        await application.bot.set_my_commands(bot_commands)
        logger.info("Daftar perintah bot (%d command) berhasil didaftarkan.", len(bot_commands))
    except Exception as exc:
        logger.warning("Gagal mendaftarkan bot commands: %s", exc)

    # Jalankan background scheduler
    scheduler.start()

    # Jalankan WhatsApp Manager jika diaktifkan di config.json
    wa_manager: Optional[WhatsAppManager] = application.bot_data.get("wa_manager")
    if wa_manager:
        wa_manager.start()
        logger.info("WhatsAppManager aktif.")

    # Jalankan Discord Bot jika diaktifkan
    discord_manager: Optional[DiscordManager] = application.bot_data.get("discord_manager")
    if discord_manager:
        asyncio.create_task(discord_manager.start_bot())
        logger.info("Discord Bot background service dijalankan.")

    logger.info("PSBOT-001 berhasil berjalan lancar!")


async def post_shutdown(application: Application) -> None:
    """Callback saat bot dimatikan secara graceful (SIGINT / SIGTERM)."""
    logger = setup_logger()
    logger.info("Menghentikan komponen PSBOT-001...")

    scheduler: BotScheduler = application.bot_data.get("scheduler")
    if scheduler:
        scheduler.stop()

    wa_manager: Optional[WhatsAppManager] = application.bot_data.get("wa_manager")
    if wa_manager:
        wa_manager.stop()

    discord_manager: Optional[DiscordManager] = application.bot_data.get("discord_manager")
    if discord_manager:
        await discord_manager.stop_bot()

    logger.info("Graceful shutdown selesai.")


def main() -> None:
    """Fungsi utama untuk menjalankan PSBOT-001."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("Loop is closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    load_dotenv()
    bot_cfg = load_bot_config("config.json")
    features_cfg = bot_cfg.get("features", {})

    log_level = os.getenv("LOG_LEVEL", "INFO")
    logger = setup_logger(level=log_level)
    print_startup_banner()
    logger.info("Memulai inisialisasi PSBOT-001...")

    # Konfigurasi otomatis Termux / Android untuk Playwright
    import platform as sys_platform
    is_arm = "arm" in sys_platform.machine().lower() or "aarch64" in sys_platform.machine().lower()
    is_termux = os.path.exists("/data/data/com.termux") or os.path.exists("/data/data/com.termux/files")

    if is_termux or sys_platform.system().lower() not in ("windows", "darwin"):
        if "PLAYWRIGHT_HOST_PLATFORM_OVERRIDE" not in os.environ:
            os.environ["PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"] = "ubuntu24.04-arm64" if is_arm else "ubuntu24.04-x64"

    if "PLAYWRIGHT_NODEJS_PATH" not in os.environ:
        import shutil
        node_candidates = [
            shutil.which("node"),
            "/data/data/com.termux/files/usr/bin/node",
            os.path.expanduser("~/../usr/bin/node"),
        ]
        node_bin = next((n for n in node_candidates if n and os.path.exists(n)), None)
        if node_bin:
            os.environ["PLAYWRIGHT_NODEJS_PATH"] = node_bin

    if is_termux:
        if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
        if "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD" not in os.environ:
            os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"

    # Redam log peringatan internal APScheduler saat job interval beririsan karena latensi jaringan
    logging.getLogger("apscheduler").setLevel(logging.ERROR)
    logging.getLogger("apscheduler.scheduler").setLevel(logging.ERROR)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.ERROR)

    # Validasi environment
    bot_token, _, admin_user_id = validate_environment()

    # Inisialisasi Database SQLite
    db = DatabaseManager("ethol_bot.db")

    # Inisialisasi WhatsApp Manager jika diaktifkan di config.json
    wa_enabled = bool(features_cfg.get("whatsapp_sync_enabled", True))
    env_wa_enable = os.getenv("ENABLE_WHATSAPP", "true").strip().lower() in ("true", "1", "yes")
    wa_phone_number = os.getenv("WA_PHONE_NUMBER", "").strip()

    wa_manager: Optional[WhatsAppManager] = None
    if wa_enabled and env_wa_enable:
        wa_manager = WhatsAppManager(
            db=db,
            session_path="wa_session",
            phone_number=wa_phone_number or None,
            loop=loop,
            admin_user_id=admin_user_id,
        )
        if wa_manager.backend:
            logger.info("WhatsApp sync aktif via Baileys (Nomor: %s).", wa_phone_number or "Scan QR")
        else:
            logger.warning(
                "WhatsApp diaktifkan tetapi Node.js / wa_bridge.js belum siap. "
                "Jalankan: npm install && node wa_bridge.js"
            )
            wa_manager = None
    else:
        logger.info("WhatsApp sync dinonaktifkan via config.json (whatsapp_sync_enabled=false).")

    # Inisialisasi ETHOL Client & Filter Engine
    client = EtholClient("config.json")
    filter_engine = FilterEngine("config.json")

    # Inisialisasi Discord Bot jika diaktifkan
    discord_cfg = bot_cfg.get("discord", {})
    discord_feature_enabled = bool(features_cfg.get("discord_sync_enabled", True)) and bool(discord_cfg.get("enabled", True))
    env_discord_enable = os.getenv("ENABLE_DISCORD", "true").strip().lower() in ("true", "1", "yes")
    discord_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    discord_channel_id = os.getenv("DISCORD_CHANNEL_ID", "").strip() or discord_cfg.get("channel_id", "")

    discord_manager: Optional[DiscordManager] = None
    if discord_feature_enabled and env_discord_enable and discord_token and "YOUR_DISCORD_BOT_TOKEN" not in discord_token:
        try:
            discord_manager = DiscordManager(
                token=discord_token,
                db=db,
                client=client,
                default_channel_id=discord_channel_id or None,
                command_prefix=discord_cfg.get("command_prefix", "!"),
                status_activity=discord_cfg.get("status_activity", "ETHOL PENS Academic"),
                admin_user_id=admin_user_id,
            )
            logger.info("Discord Bot terkonfigurasi (Prefix: '%s').", discord_cfg.get("command_prefix", "!"))
        except Exception as exc:
            logger.error("Gagal inisialisasi Discord Bot: %s", exc)
            discord_manager = None
    else:
        if not discord_token or "YOUR_DISCORD_BOT_TOKEN" in discord_token:
            logger.info("Discord Bot dinonaktifkan (DISCORD_BOT_TOKEN belum diset di .env).")
        else:
            logger.info("Discord Bot dinonaktifkan via config.json (discord_sync_enabled=false).")

    # Bangun Telegram application dengan konfigurasi timeout yang tangguh terhadap lonjakan jaringan
    tg_request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=30.0,
        write_timeout=20.0,
        pool_timeout=10.0,
        connection_pool_size=16,
    )
    application = (
        ApplicationBuilder()
        .token(bot_token)
        .request(tg_request)
        .get_updates_request(tg_request)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    notifier = Notifier(
        bot=application.bot,
        db=db,
        wa_client=wa_manager,
        ethol_client=client,
        config_path="config.json",
        discord_client=discord_manager,
    )
    if wa_manager:
        wa_manager.tg_notifier = notifier

    scheduler = BotScheduler(
        client=client,
        filter_engine=filter_engine,
        notifier=notifier,
        db=db,
        config_path="config.json",
    )

    application.bot_data["db"] = db
    application.bot_data["client"] = client
    application.bot_data["filter_engine"] = filter_engine
    application.bot_data["notifier"] = notifier
    application.bot_data["scheduler"] = scheduler
    application.bot_data["wa_manager"] = wa_manager
    application.bot_data["discord_manager"] = discord_manager
    application.bot_data["admin_user_id"] = admin_user_id
    application.bot_data["start_time"] = time.time()

    # Registrasi Command Handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("login", cmd_login))
    application.add_handler(CommandHandler("logout", cmd_logout))
    application.add_handler(CommandHandler("connect", cmd_connect))
    application.add_handler(CommandHandler("disconnect", cmd_disconnect))
    application.add_handler(CommandHandler("unconnect", cmd_disconnect))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("absen", cmd_absen))
    application.add_handler(CommandHandler("tugas", cmd_tugas))
    application.add_handler(CommandHandler("materi", cmd_materi))
    application.add_handler(CommandHandler("matkul", cmd_matkul))
    application.add_handler(CommandHandler("detail", cmd_detail))
    application.add_handler(CommandHandler("deadline", cmd_deadline))
    application.add_handler(CommandHandler("filter", cmd_filter))
    application.add_handler(CommandHandler("mute", cmd_mute))
    application.add_handler(CommandHandler("unmute", cmd_unmute))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("skem", cmd_skem))

    # Registrasi Callback Query Handler
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Registrasi Error Handler
    application.add_error_handler(error_handler)

    logger.info("Menjalankan Telegram long polling...")
    application.run_polling(
        drop_pending_updates=True,
        close_loop=False,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
