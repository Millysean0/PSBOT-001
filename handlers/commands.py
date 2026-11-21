"""Handler perintah Telegram untuk ethol-bot (Mendukung Multi-User, SQLite, & CAS SSO).

Mengimplementasikan seluruh command:
/start, /login, /logout, /status, /absen, /tugas, /materi,
/matkul, /detail, /deadline, /filter, /mute, /unmute, /pause, /resume, /debug.
Setiap pengguna Telegram memiliki sesi, kredensial terenkripsi, filter,
dan riwayat terpisah di database SQLite dengan autentikasi CAS SSO (login.pens.ac.id)
dan terintegrasi langsung dengan REST API portal ETHOL PENS.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    import psutil
except ImportError:
    psutil = None
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from core.database import DatabaseManager
from core.ethol_client import EtholClient
from core.filter_engine import FilterEngine
from core.notifier import Notifier
from utils.crypto import decrypt_string, encrypt_string
from utils.logger import setup_logger

logger = setup_logger(__name__)


def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Membuat inline keyboard menu utama untuk navigasi cepat."""
    keyboard = [
        [
            InlineKeyboardButton("📊 Status", callback_data="btn_status"),
            InlineKeyboardButton("📝 Presensi & Auto-Absen", callback_data="btn_absen"),
        ],
        [
            InlineKeyboardButton("📚 Tugas Aktif", callback_data="btn_tugas"),
            InlineKeyboardButton("⏰ Deadline (≤3 Hari)", callback_data="btn_deadline"),
        ],
        [
            InlineKeyboardButton("📖 Mata Kuliah", callback_data="btn_matkul"),
            InlineKeyboardButton("📎 Materi Kuliah", callback_data="btn_materi"),
        ],
        [
            InlineKeyboardButton("📱 Tautkan WhatsApp", callback_data="btn_connect_wa"),
            InlineKeyboardButton("⚙️ Filter & Watchlist", callback_data="btn_filter_info"),
        ],
        [
            InlineKeyboardButton("🔕 Mute Notif", callback_data="btn_mute_menu"),
            InlineKeyboardButton("⏯️ Jeda / Lanjut Polling", callback_data="btn_pause_toggle"),
        ],
        [
            InlineKeyboardButton("🛠️ Debug HTML", callback_data="btn_debug"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_courses_keyboard(courses: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    """Membuat tombol pilihan mata kuliah untuk melihat detail matkul."""
    keyboard: List[List[InlineKeyboardButton]] = []
    # Buat 2 tombol per baris jika nama pendek, atau 1 per baris
    row: List[InlineKeyboardButton] = []
    for idx, c in enumerate(courses, start=1):
        nomor = c.get("nomor")
        nama = c.get("nama", f"MK {idx}")
        label = (nama[:22] + "…") if len(nama) > 25 else nama
        btn = InlineKeyboardButton(f"📖 {idx}. {label}", callback_data=f"detail_mk_{nomor}")
        keyboard.append([btn])

    keyboard.append([InlineKeyboardButton("🔙 Menu Utama", callback_data="btn_menu_utama")])
    return InlineKeyboardMarkup(keyboard)


async def _get_authenticated_user_credentials(user_id: int, db: DatabaseManager, client: EtholClient) -> Tuple[bool, str, str]:
    """Helper untuk mengambil kredensial, mendekripsi, dan memastikan session login user aktif."""
    user_data = db.get_user(user_id)
    if not user_data or not user_data.get("is_logged_in"):
        return False, "", ""

    enc_u = user_data.get("encrypted_username", "")
    enc_p = user_data.get("encrypted_password", "")
    if not enc_u or not enc_p:
        return False, "", ""

    try:
        u = decrypt_string(enc_u)
        p = decrypt_string(enc_p)
        is_ok = client.ensure_session(user_id, u, p)
        return is_ok, u, p
    except Exception as exc:
        logger.error("[User %s] Gagal mendekripsi kredensial atau memastikan session: %s", user_id, exc)
        return False, "", ""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /start: Mendaftarkan user ke database dan menampilkan menu interaktif."""
    user = update.effective_user
    if not user:
        return

    db: DatabaseManager = context.bot_data["db"]
    user_data = db.get_or_create_user(
        user_id=user.id,
        telegram_username=user.username,
        first_name=user.first_name,
    )

    is_logged_in = bool(user_data.get("is_logged_in", 0))
    nrp_display = user_data.get("ethol_username_display", "")
    login_badge = f"🟢 Terhubung (<code>{html.escape(nrp_display)}</code>)" if is_logged_in and nrp_display else ("🟢 Terhubung" if is_logged_in else "🔴 Belum Login (/login)")

    text = (
        f"🤖 <b>ETHOL PENS Telegram Assistant (CAS SSO)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Halo, <b>{html.escape(user.first_name or 'Mahasiswa')}</b>! 👋\n"
        f"Bot ini berjalan secara multi-user dengan konsumsi RAM rendah (&lt; 150 MB) "
        f"dan terhubung langsung ke sistem autentikasi <b>CAS PENS (login.pens.ac.id)</b> "
        f"serta REST API portal <b>ETHOL PENS</b>.\n\n"
        f"📌 <b>Status Akun Anda:</b> {login_badge}\n"
        f"⏱️ <b>Interval Polling:</b> 30 detik (Auto-absen & Notifikasi aktif)\n\n"
        f"Gunakan menu interaktif di bawah atau ketik perintah langsung:"
    )

    if update.effective_message:
        await update.effective_message.reply_text(
            text,
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /login: Input email/NetID & password, hapus pesan otomatis, enkripsi Fernet, login CAS SSO."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    args = context.args or []

    if len(args) < 2:
        help_text = (
            "🔐 <b>Cara Login ke Akun ETHOL PENS (CAS SSO):</b>\n\n"
            "Portal ETHOL menggunakan autentikasi terpusat <b>EEPIS CAS (login.pens.ac.id)</b>.\n\n"
            "Kirim perintah dengan format:\n"
            "<code>/login &lt;email_atau_netid&gt; &lt;password&gt;</code>\n\n"
            "<i>Contoh Penggunaan:</i>\n"
            "• <code>/login mahasiswa@it.student.pens.ac.id Rahasia123</code>\n"
            "• <code>/login mahasiswa Rahasia123</code>\n\n"
            "<i>🛡️ Demi keamanan privasi, pesan perintah yang Anda kirim akan "
            "<b>langsung dihapus otomatis</b> oleh bot dalam hitungan milidetik.</i>"
        )
        await message.reply_text(help_text, parse_mode=ParseMode.HTML)
        return

    username = args[0].strip()
    password = " ".join(args[1:]).strip()

    # Hapus pesan pengguna yang memuat password plaintext
    try:
        await message.delete()
        logger.info("[User %s] Pesan kredensial berhasil dihapus dari chat.", user.id)
    except Exception as exc:
        logger.warning("[User %s] Gagal menghapus pesan kredensial: %s", user.id, exc)

    loading_msg = await update.effective_chat.send_message(
        "🔄 <i>Memverifikasi login via EEPIS Central Authentication Service (login.pens.ac.id)... Mohon tunggu.</i>",
        parse_mode=ParseMode.HTML,
    )

    client: EtholClient = context.bot_data["client"]
    db: DatabaseManager = context.bot_data["db"]

    # Pastikan user terdaftar di database
    db.get_or_create_user(user.id, user.username, user.first_name)

    # Uji login ke CAS PENS
    success, msg = client.login(user_id=user.id, username=username, password=password)

    if success:
        # Enkripsi kredensial sebelum disimpan ke database SQLite
        enc_u = encrypt_string(username)
        enc_p = encrypt_string(password)
        db.update_user_credentials(
            user_id=user.id,
            enc_username=enc_u,
            enc_password=enc_p,
            ethol_display=username,
        )

        profile = client.get_user_profile(user.id) or {}
        nama_mhs = profile.get("nama", username)
        nrp = profile.get("nipnrp", "")

        await loading_msg.edit_text(
            f"✅ <b>Login CAS SSO Berhasil!</b>\n\n"
            f"👤 <b>Nama:</b> <b>{html.escape(nama_mhs)}</b>\n"
            f"🆔 <b>NRP/NetID:</b> <code>{html.escape(nrp or username)}</code>\n"
            f"🔐 Kredensial Anda telah diamankan dengan <b>Fernet (AES)</b> di database lokal.\n"
            f"🚀 Sesi portal ETHOL kini aktif! Background polling 30 detik & auto-absen berjalan otomatis.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await loading_msg.edit_text(
            f"❌ <b>Login CAS Gagal!</b>\n\n"
            f"Penyebab: <i>{html.escape(msg)}</i>\n\n"
            f"Pastikan NetID / Email PENS dan password Anda sudah benar (sesuai akun login.pens.ac.id), lalu coba lagi dengan:\n"
            f"<code>/login &lt;email_atau_netid&gt; &lt;password&gt;</code>",
            parse_mode=ParseMode.HTML,
        )


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /logout: Menghapus kredensial tersimpan di database dan membersihkan session pool."""
    user = update.effective_user
    if not user:
        return

    db: DatabaseManager = context.bot_data["db"]
    client: EtholClient = context.bot_data["client"]

    db.logout_user(user.id)
    client.clear_session(user.id)

    if update.effective_message:
        await update.effective_message.reply_text(
            "🔒 <b>Berhasil Logout</b>\n\n"
            "Kredensial akun PENS Anda telah dihapus dari database dan session dibersihkan. "
            "Polling otomatis dinonaktifkan sampai Anda login kembali dengan /login.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /status: Menampilkan status akun user, polling, RAM usage (psutil), uptime, dan statistik database."""
    user = update.effective_user
    if not user:
        return

    db: DatabaseManager = context.bot_data["db"]
    client: EtholClient = context.bot_data["client"]
    start_time: float = context.bot_data["start_time"]
    user_data = db.get_or_create_user(user.id, user.username, user.first_name)

    # 1. Metrik resource sistem (psutil opsional untuk Termux)
    if psutil:
        process = psutil.Process(os.getpid())
        ram_bytes = process.memory_info().rss
        ram_mb = ram_bytes / (1024 * 1024)
        ram_percent = process.memory_percent()
        cpu_percent = process.cpu_percent(interval=None)
    else:
        ram_mb = 0.0
        ram_percent = 0.0
        cpu_percent = 0.0

    # 2. Uptime bot
    uptime_sec = int(time.time() - start_time)
    days, rem = divmod(uptime_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime_str = f"{days}h {hours}j {minutes}m {seconds}d" if days else f"{hours}j {minutes}m {seconds}d"

    # 3. Status user individual
    is_logged = bool(user_data.get("is_logged_in", 0))
    display_user = user_data.get("ethol_username_display", "")

    profile = client.get_user_profile(user.id) if is_logged else None
    if profile:
        mhs_name = profile.get("nama", display_user)
        mhs_nrp = profile.get("nipnrp", "")
        login_str = f"🟢 Aktif (<b>{html.escape(mhs_name)}</b> - <code>{html.escape(mhs_nrp)}</code>)"
    elif is_logged:
        login_str = f"🟢 Aktif (<code>{html.escape(display_user)}</code>)"
    else:
        login_str = "🔴 Belum Login (/login)"

    is_paused = bool(user_data.get("is_paused", 0))
    polling_str = "⏸️ Dijeda (/resume)" if is_paused else "▶️ Berjalan (30 detik)"

    is_auto_absen = bool(user_data.get("auto_absen_enabled", 1))
    auto_absen_str = "🟢 Aktif (/absen off)" if is_auto_absen else "🔴 Nonaktif (/absen on)"

    last_poll = user_data.get("last_poll_at")
    if last_poll:
        try:
            dt = datetime.fromisoformat(last_poll)
            last_poll_str = dt.strftime("%d-%m-%Y %H:%M:%S")
        except Exception:
            last_poll_str = str(last_poll)
    else:
        last_poll_str = "Belum pernah"

    ram_status = "✅ &lt; 150 MB" if ram_mb < 150 else "⚠️ Mendekati limit"

    # 4. Statistik database multi-user
    stats = db.get_stats()
    sent_count = user_data.get("sent_count_total", 0)

    text = (
        f"📊 <b>STATUS AKUN & SISTEM ETHOL-BOT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Status Akun Anda:</b> {login_str}\n"
        f"🔄 <b>Background Polling:</b> {polling_str}\n"
        f"⚡ <b>Auto-Absen Presensi:</b> {auto_absen_str}\n"
        f"🕒 <b>Polling Terakhir:</b> <code>{last_poll_str}</code>\n"
        f"📬 <b>Notifikasi Diterima:</b> {sent_count} pesan\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Total Pengguna:</b> {stats.get('total_users', 0)} orang\n"
        f"🟢 <b>Pengguna Aktif:</b> {stats.get('active_users', 0)} orang\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💾 <b>RAM Usage (RSS):</b> <code>{ram_mb:.2f} MB</code> ({ram_status})\n"
        f"📈 <b>RAM % Sistem:</b> <code>{ram_percent:.1f}%</code>\n"
        f"⚙️ <b>CPU Usage:</b> <code>{cpu_percent:.1f}%</code>\n"
        f"⏱️ <b>Bot Uptime:</b> {uptime_str}\n"
    )

    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


def get_absen_keyboard(
    user_id: int,
    is_auto: bool,
    active_sessions: Optional[List[Dict[str, Any]]] = None,
) -> InlineKeyboardMarkup:
    """Membuat inline keyboard interaktif untuk menu manajemen presensi & auto-absen."""
    keyboard: List[List[InlineKeyboardButton]] = []

    # Baris 1: Tombol Toggle Auto-Absen
    if is_auto:
        toggle_btn = InlineKeyboardButton("🔴 Matikan Auto-Absen", callback_data="toggle_auto_absen")
    else:
        toggle_btn = InlineKeyboardButton("🟢 Aktifkan Auto-Absen", callback_data="toggle_auto_absen")
    keyboard.append([toggle_btn])

    # Baris 2: Tombol Aksi Manual & Refresh
    keyboard.append([
        InlineKeyboardButton("⚡ Absen Sekarang", callback_data="btn_absen_manual_now"),
        InlineKeyboardButton("🔄 Segarkan Sesi", callback_data="btn_absen_refresh"),
    ])

    # Baris 3: Tombol Presensi Cepat per MK yang terbuka (jika ada)
    if active_sessions:
        for s in active_sessions[:3]:
            kuliah_id = s.get("kuliah")
            mk_name = s.get("mk_name", "Mata Kuliah")
            short_name = (mk_name[:20] + "…") if len(mk_name) > 23 else mk_name
            keyboard.append([
                InlineKeyboardButton(f"📝 Presensi: {short_name}", callback_data=f"absen_mk_{kuliah_id}")
            ])

    # Baris 4: Riwayat & Navigasi
    keyboard.append([
        InlineKeyboardButton("📋 Riwayat Absen", callback_data="btn_absen_history"),
        InlineKeyboardButton("🔙 Menu Utama", callback_data="btn_menu_utama"),
    ])
    return InlineKeyboardMarkup(keyboard)


def render_absen_dashboard(
    user_id: int,
    db: DatabaseManager,
    client: EtholClient,
    active_sessions: Optional[List[Dict[str, Any]]] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Menyusun teks dashboard dan keyboard kontrol presensi serta auto-absen."""
    is_auto = db.is_auto_absen_enabled(user_id)
    status_badge = "🟢 <b>AKTIF</b>" if is_auto else "🔴 <b>NONAKTIF</b>"

    profile = client.get_user_profile(user_id)
    mhs_name = profile.get("nama") if profile else None
    mhs_nrp = profile.get("nipnrp") if profile else None

    if mhs_name and mhs_nrp:
        user_line = f"👤 <b>Mahasiswa:</b> {html.escape(mhs_name)} (<code>{html.escape(mhs_nrp)}</code>)"
    elif mhs_name:
        user_line = f"👤 <b>Mahasiswa:</b> {html.escape(mhs_name)}"
    else:
        user_line = "👤 <b>Mahasiswa:</b> Terverifikasi"

    if active_sessions is None:
        try:
            active_sessions = client.fetch_presensi_active(user_id)
        except Exception as exc:
            logger.warning("[User %s] Gagal memeriksa presensi aktif: %s", user_id, exc)
            active_sessions = []

    sess_lines: List[str] = []
    if active_sessions:
        sess_lines.append(f"🟢 <b>Ditemukan {len(active_sessions)} sesi perkuliahan TERBUKA:</b>")
        for idx, s in enumerate(active_sessions, start=1):
            mk_name = html.escape(s.get("mk_name", "Mata Kuliah"))
            dosen = html.escape(s.get("dosen", "-"))
            sess_lines.append(f"  {idx}. <b>{mk_name}</b>\n     👨‍🏫 <i>Dosen: {dosen}</i>")
    else:
        sess_lines.append("ℹ️ <i>Tidak ada sesi presensi yang sedang terbuka di portal ETHOL saat ini.</i>")
        if is_auto:
            sess_lines.append("<i>Bot otomatis memantau dan mencatat presensi saat dosen membuka sesi kuliah.</i>")
        else:
            sess_lines.append("<i>Aktifkan auto-absen agar bot otomatis presensi saat sesi dibuka.</i>")

    sess_text = "\n".join(sess_lines)

    # Ambil riwayat log terbaru dari database
    logs = db.get_recent_presensi_logs(user_id, limit=3)
    log_lines: List[str] = []
    if logs:
        for l in logs:
            st = l.get("status", "")
            icon = "✅" if "SUCCESS" in st or st == "ALREADY" else "❌"
            dt_str = ""
            try:
                dt = datetime.fromisoformat(l["created_at"])
                dt_str = dt.strftime("%d/%m %H:%M")
            except Exception:
                dt_str = l.get("created_at", "")[:16]
            mode = "Auto" if "MANUAL" not in st else "Manual"
            log_lines.append(f"• {icon} <code>{dt_str}</code> <b>{html.escape(l.get('mk_name', ''))}</b> ({mode})")
        log_text = "\n".join(log_lines)
    else:
        log_text = "<i>Belum ada riwayat presensi tercatat di database bot.</i>"

    text = (
        f"📝 <b>PENGATURAN PRESENSI & AUTO-ABSEN</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{user_line}\n"
        f"⚡ <b>Status Auto-Absen:</b> {status_badge}\n"
        f"⏱️ <b>Interval Polling:</b> Setiap 30 detik\n\n"
        f"📡 <b>Status Sesi Kuliah:</b>\n"
        f"{sess_text}\n\n"
        f"📋 <b>Aktivitas Presensi Terakhir:</b>\n"
        f"{log_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <b>Shortcut Perintah:</b>\n"
        f"• <code>/absen on</code> — Aktifkan auto-absen\n"
        f"• <code>/absen off</code> — Matikan auto-absen\n"
        f"• <code>/absen manual</code> — Eksekusi absen manual sekarang\n"
        f"• <code>/absen &lt;kode_mk&gt;</code> — Presensi manual MK tertentu\n"
    )

    keyboard = get_absen_keyboard(user_id, is_auto, active_sessions)
    return text, keyboard


async def execute_manual_absen(
    user_id: int,
    client: EtholClient,
    db: DatabaseManager,
    target_mk: Optional[str] = None,
) -> tuple[bool, str]:
    """Eksekusi presensi manual untuk akun pengguna dan mencatatnya ke database."""
    active_sessions = client.fetch_presensi_active(user_id)
    if not active_sessions:
        return (
            False,
            "ℹ️ <b>Tidak ada sesi presensi yang sedang terbuka</b> di portal ETHOL saat ini.\n"
            "Jika fitur auto-absen aktif, bot akan otomatis mencatat presensi setiap kali dosen membuka sesi perkuliahan.",
        )

    results: List[str] = []
    success_any = False
    now = time.time()

    for sess in active_sessions:
        mk_name = sess.get("mk_name", "")
        mk_code = str(sess.get("mk_code", ""))
        key = str(sess.get("key", ""))

        if target_mk and (target_mk not in mk_code.lower() and target_mk not in mk_name.lower()):
            continue

        res = client.submit_absen(user_id, sess)
        success = res.get("success", False)
        already_attended = res.get("already_attended", False)
        msg = res.get("message", "")

        status_log = "MANUAL_SUCCESS" if (success and not already_attended) else ("ALREADY" if already_attended else "MANUAL_FAILED")
        db.log_presensi(user_id, mk_code, mk_name, key, status_log, msg)

        if success or already_attended:
            success_any = True
            db.set_presensi_cooldown(user_id, mk_code, now + 43200)

        icon = "✅" if (success or already_attended) else "❌"
        results.append(f"{icon} <b>{html.escape(mk_name)}</b>: {html.escape(msg)}")

    if not results and target_mk:
        return (
            False,
            f"⚠️ Tidak ditemukan sesi presensi aktif yang cocok dengan <code>{html.escape(target_mk)}</code>.",
        )

    out_text = (
        "📝 <b>HASIL PRESENSI MANUAL:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(results) + "\n━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Ketik /absen untuk membuka kembali dashboard kontrol presensi.</i>"
    )
    return success_any, out_text


async def show_presensi_history(message: Any, user_id: int, db: DatabaseManager) -> None:
    """Menampilkan riwayat presensi yang telah dicatat bot untuk user."""
    logs = db.get_recent_presensi_logs(user_id, limit=10)
    lines = [
        "📋 <b>RIWAYAT PRESENSI BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Berikut 10 riwayat aktivitas presensi terakhir yang tercatat:"
    ]
    if logs:
        for idx, l in enumerate(logs, start=1):
            st = l.get("status", "")
            icon = "✅" if "SUCCESS" in st or st == "ALREADY" else "❌"
            dt_str = ""
            try:
                dt = datetime.fromisoformat(l["created_at"])
                dt_str = dt.strftime("%d-%m-%Y %H:%M:%S")
            except Exception:
                dt_str = l.get("created_at", "")
            mode = "Auto-Absen" if "MANUAL" not in st else "Manual"
            lines.append(
                f"{idx}. {icon} <b>{html.escape(l.get('mk_name', 'Mata Kuliah'))}</b>\n"
                f"   ⏰ <code>{html.escape(dt_str)}</code> ({mode})\n"
                f"   ℹ️ <i>{html.escape(l.get('message', '-'))}</i>"
            )
    else:
        lines.append("\n<i>Belum ada riwayat presensi yang tercatat di database bot.</i>")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Dashboard Presensi", callback_data="btn_absen")],
        [InlineKeyboardButton("🔙 Menu Utama", callback_data="btn_menu_utama")],
    ])

    await message.reply_text("\n\n".join(lines), reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def cmd_absen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /absen [on|off|toggle|manual|riwayat|<kode_mk>]: Mengelola presensi & auto-absen."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    client: EtholClient = context.bot_data["client"]
    db: DatabaseManager = context.bot_data["db"]

    is_auth, _, _ = await _get_authenticated_user_credentials(user.id, db, client)
    if not is_auth:
        await message.reply_text("⚠️ Anda belum login. Silakan gunakan /login terlebih dahulu.", parse_mode=ParseMode.HTML)
        return

    args = context.args or []

    # 1. Subcommand /absen on / aktif / enable
    if args and args[0].strip().lower() in ["on", "aktif", "enable", "start", "1"]:
        db.set_auto_absen_enabled(user.id, True)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Dashboard Presensi", callback_data="btn_absen")],
            [InlineKeyboardButton("🔙 Menu Utama", callback_data="btn_menu_utama")],
        ])
        await message.reply_text(
            "✅ <b>Fitur Auto-Absen Berhasil Diaktifkan!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 <b>Status: 🟢 AKTIF</b>\n"
            "Bot akan memantau portal ETHOL setiap <b>30 detik</b> di background.\n\n"
            "Ketika dosen membuka sesi presensi untuk kelas Anda:\n"
            "• Bot akan otomatis melakukan presensi untuk Anda.\n"
            "• Notifikasi konfirmasi akan langsung dikirimkan ke chat Telegram ini.\n"
            "• Dilengkapi proteksi anti-spam & cooldown otomatis.\n\n"
            "<i>Ketik <code>/absen off</code> kapan saja jika ingin menonaktifkannya.</i>",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
        return

    # 2. Subcommand /absen off / nonaktif / disable
    if args and args[0].strip().lower() in ["off", "nonaktif", "disable", "stop", "0"]:
        db.set_auto_absen_enabled(user.id, False)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Aktifkan Auto-Absen", callback_data="toggle_auto_absen")],
            [InlineKeyboardButton("📝 Dashboard Presensi", callback_data="btn_absen")],
            [InlineKeyboardButton("🔙 Menu Utama", callback_data="btn_menu_utama")],
        ])
        await message.reply_text(
            "⏸️ <b>Fitur Auto-Absen Berhasil Dinonaktifkan</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 <b>Status: 🔴 NONAKTIF</b>\n"
            "Bot <b>tidak akan</b> melakukan absensi otomatis di latar belakang.\n\n"
            "💡 <i>Anda tetap dapat melakukan presensi secara manual kapan saja dengan perintah <code>/absen manual</code> atau tombol di menu /absen.</i>\n\n"
            "<i>Ketik <code>/absen on</code> untuk mengaktifkan kembali auto-absen.</i>",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
        return

    # 3. Subcommand /absen toggle
    if args and args[0].strip().lower() == "toggle":
        new_val = db.toggle_auto_absen(user.id)
        status_str = "🟢 <b>AKTIF</b>" if new_val else "🔴 <b>NONAKTIF</b>"
        await message.reply_text(
            f"🔄 Status auto-absen berhasil diubah menjadi: {status_str}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 Buka Dashboard Presensi", callback_data="btn_absen")]]),
            parse_mode=ParseMode.HTML,
        )
        return

    # 4. Subcommand /absen riwayat / history / log
    if args and args[0].strip().lower() in ["riwayat", "history", "log", "logs"]:
        await show_presensi_history(message, user.id, db)
        return

    # 5. Subcommand /absen manual / now / scan ATAU spesifik MK (/absen <kode_mk>)
    if args:
        target = args[0].strip().lower()
        target_mk = None if target in ["manual", "now", "scan", "cek", "submit"] else target
        if target in ["manual", "now", "scan", "cek", "submit"] and len(args) > 1:
            target_mk = args[1].strip().lower()

        wait_msg = await message.reply_text("🔍 <i>Menjalankan pengecekan presensi di portal ETHOL...</i>", parse_mode=ParseMode.HTML)
        _, report_text = await execute_manual_absen(user.id, client, db, target_mk=target_mk)
        await wait_msg.edit_text(report_text, parse_mode=ParseMode.HTML)
        return

    # 6. Default: /absen tanpa argumen -> Tampilkan Dashboard Presensi & Auto-Absen
    wait_msg = await message.reply_text("🔍 <i>Memuat dashboard presensi...</i>", parse_mode=ParseMode.HTML)
    dashboard_text, keyboard = render_absen_dashboard(user.id, db, client)
    await wait_msg.edit_text(dashboard_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def cmd_tugas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /tugas [all]: Menampilkan daftar tugas kuliah pengguna dari portal ETHOL."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    client: EtholClient = context.bot_data["client"]
    db: DatabaseManager = context.bot_data["db"]

    is_auth, _, _ = await _get_authenticated_user_credentials(user.id, db, client)
    if not is_auth:
        await message.reply_text("⚠️ Anda belum login. Silakan gunakan /login terlebih dahulu.", parse_mode=ParseMode.HTML)
        return

    args = context.args or []
    include_all = len(args) > 0 and args[0].lower() == "all"

    wait_msg = await message.reply_text("🔄 <i>Mengambil data tugas dari ETHOL Anda...</i>", parse_mode=ParseMode.HTML)
    tasks = client.fetch_tugas(user_id=user.id, include_all=include_all)

    if not tasks:
        if not include_all:
            msg = (
                "🎉 <b>Semua Tugas Selesai!</b>\n\n"
                "Tidak ada tugas aktif yang belum dikumpulkan saat ini.\n"
                "Ketik <code>/tugas all</code> untuk melihat riwayat tugas yang sudah dikumpulkan."
            )
        else:
            msg = "ℹ️ Tidak ditemukan riwayat tugas perkuliahan pada semester aktif."
        await wait_msg.edit_text(msg, parse_mode=ParseMode.HTML)
        return

    header = "📚 <b>SEMUA RIWAYAT TUGAS PERKULIAHAN:</b>" if include_all else "📚 <b>DAFTAR TUGAS AKTIF (BELUM DIKUMPULKAN):</b>"
    lines = [f"{header}\n━━━━━━━━━━━━━━━━━━━━━━━━"]

    for idx, t in enumerate(tasks, start=1):
        judul = html.escape(t.get("judul", "-"))
        matkul = html.escape(t.get("matkul", "-"))
        dosen = html.escape(t.get("dosen", "-"))
        deadline = html.escape(t.get("deadline", "-"))
        deskripsi = t.get("deskripsi", "")

        is_sub = t.get("is_submitted")
        hours_left = t.get("hours_left", 9999)
        is_overdue = t.get("is_overdue", False)

        if is_sub:
            sub_time = html.escape(t.get("submission_time") or "Sudah dikumpulkan")
            status_line = f"🟢 <b>Sudah Dikumpulkan:</b> <code>{sub_time}</code>"
            if t.get("grade") is not None:
                status_line += f" | ⭐ <b>Nilai:</b> <code>{t['grade']}</code>"
        elif is_overdue:
            status_line = "⚠️ <b>SUDAH LEWAT DEADLINE!</b> (Belum dikumpulkan)"
        elif hours_left <= 24:
            status_line = f"🔥 <b>KRITIS: Sisa {hours_left} Jam!</b> (Belum dikumpulkan)"
        elif hours_left <= 72:
            days = round(hours_left / 24, 1)
            status_line = f"⚡ <b>Segera: Sisa ~{days} hari</b> ({hours_left} jam)"
        else:
            days = round(hours_left / 24, 1)
            status_line = f"⏳ <b>Batas Waktu:</b> Sisa ~{days} hari ({hours_left} jam)"

        block = (
            f"<b>{idx}. {judul}</b>\n"
            f"   📖 <b>Matkul:</b> {matkul}\n"
            f"   👨‍🏫 <b>Dosen:</b> {dosen}\n"
            f"   ⏰ <b>Deadline:</b> <code>{deadline}</code>\n"
            f"   📌 {status_line}"
        )

        # Deskripsi ringkas jika ada
        if deskripsi and not is_sub:
            clean_desc = re.sub(r"\s+", " ", deskripsi).strip()
            if len(clean_desc) > 120:
                clean_desc = clean_desc[:120] + "…"
            block += f"\n   📝 <i>Catatan: {html.escape(clean_desc)}</i>"

        # Lampiran file soal jika ada
        files = t.get("files", [])
        if files:
            file_links = [f"<a href='{f['url']}'>{html.escape(f['nama'])}</a>" for f in files[:2]]
            block += f"\n   📎 <b>File Soal:</b> {', '.join(file_links)}"

        lines.append(block)

    if not include_all:
        lines.append("💡 <i>Ketik /tugas all untuk melihat semua riwayat tugas kuliah yang sudah selesai.</i>")

    text_out = "\n\n".join(lines)
    # Split text if exceeds 4000 chars
    notifier: Notifier = context.bot_data["notifier"]
    chunks = notifier.split_message(text_out)
    await wait_msg.edit_text(chunks[0], parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    for c in chunks[1:]:
        await update.effective_chat.send_message(c, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_matkul(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /matkul [query]: Menampilkan daftar mata kuliah semester aktif pengguna atau membuka detail."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    client: EtholClient = context.bot_data["client"]
    db: DatabaseManager = context.bot_data["db"]

    is_auth, _, _ = await _get_authenticated_user_credentials(user.id, db, client)
    if not is_auth:
        await message.reply_text("⚠️ Anda belum login. Silakan gunakan /login terlebih dahulu.", parse_mode=ParseMode.HTML)
        return

    args = context.args or []
    if args:
        # User mencari detail mata kuliah spesifik: /matkul basis data atau /matkul 11114
        query = " ".join(args).strip()
        await render_detail_matkul(update, context, query)
        return

    wait_msg = await message.reply_text("🔄 <i>Mengambil daftar mata kuliah semester ini...</i>", parse_mode=ParseMode.HTML)
    courses = client.fetch_matkul(user.id)

    if not courses:
        await wait_msg.edit_text("ℹ️ Tidak ditemukan daftar mata kuliah aktif pada semester ini.", parse_mode=ParseMode.HTML)
        return

    lines = [
        "📖 <b>MATA KULIAH SEMESTER INI:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    for idx, c in enumerate(courses, start=1):
        nama_mk = html.escape(c.get("nama", "-"))
        dosen = html.escape(c.get("dosen", "-"))
        jadwal = html.escape(c.get("jadwal_display", "-"))
        ruang = html.escape(c.get("ruang", "-"))
        kelas = html.escape(f"{c.get('kode_kelas', '')} {c.get('pararel', '')}".strip() or "-")

        lines.append(
            f"<b>{idx}. {nama_mk}</b>\n"
            f"   👨‍🏫 {dosen}\n"
            f"   📅 {jadwal} | 🏛 {ruang}\n"
            f"   🔖 Kelas: <code>{kelas}</code>"
        )

    lines.append(
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>Pilih mata kuliah di bawah</b> untuk melihat detail lengkap (History Presensi, RPS, Jadwal, Tugas):"
    )

    await wait_msg.edit_text(
        "\n\n".join(lines),
        reply_markup=get_courses_keyboard(courses),
        parse_mode=ParseMode.HTML,
    )


async def render_detail_matkul(update: Update, context: ContextTypes.DEFAULT_TYPE, target_mk: Any) -> None:
    """Helper utama untuk merender tampilan DETAIL MATA KULIAH sesuai format resmi ETHOL PENS (seperti di screenshot)."""
    user = update.effective_user
    if not user:
        return

    client: EtholClient = context.bot_data["client"]
    db: DatabaseManager = context.bot_data["db"]

    is_auth, _, _ = await _get_authenticated_user_credentials(user.id, db, client)
    if not is_auth:
        if update.effective_message:
            await update.effective_message.reply_text("⚠️ Anda belum login. Silakan /login terlebih dahulu.", parse_mode=ParseMode.HTML)
        return

    detail = client.get_detail_matkul(user.id, target_mk)
    if not detail:
        msg = f"⚠️ Mata kuliah <code>{html.escape(str(target_mk))}</code> tidak ditemukan. Gunakan /matkul untuk melihat daftar."
        if update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text(msg, parse_mode=ParseMode.HTML)
        elif update.effective_message:
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    course = detail["course"]
    kuliah_id = course.get("nomor")
    nama_mk = html.escape(course.get("nama", "-"))
    dosen = html.escape(course.get("dosen", "-"))
    nip = html.escape(str(course.get("nip_dosen", "-")))
    jadwal = html.escape(course.get("jadwal_display", "-"))
    ruang = html.escape(course.get("ruang", "-"))
    kelas = html.escape(f"{course.get('kode_kelas', '')} {course.get('pararel', '')}".strip() or "-")

    presensi_terakhir = html.escape(detail.get("presensi_terakhir", "-"))
    is_open = detail.get("is_presensi_open", False)
    status_presensi_badge = "🟢 <b>SEDANG DIBUKA!</b>" if is_open else "🔴 <b>Ditutup</b>"

    # Dokumen Perkuliahan
    kontrak_status = "✅ <b>Sudah Disetujui</b>" if detail.get("kontrak_disetujui") else "⚠️ <b>Belum Disetujui</b>"
    rps_nama = html.escape(detail.get("rps_nama", "Belum ada RPS"))
    rps_url = detail.get("rps_url", "")
    if rps_url:
        rps_display = f"<a href='{rps_url}'>{rps_nama}</a> (📄 Lihat/Unduh RPS)"
    else:
        rps_display = f"<code>{rps_nama}</code>"

    # History Presensi
    history = detail.get("history_presensi", [])
    total_hadir = detail.get("total_kehadiran", 0)

    history_lines: List[str] = []
    if history:
        for idx, h in enumerate(history[:5], start=1):
            h_id = h.get("nomor", "-")
            h_tgl = html.escape(h.get("waktu_indonesia") or h.get("tanggal") or "-")
            history_lines.append(f"  • <code>{h_id}</code> | {h_tgl}")
        if total_hadir > 5:
            history_lines.append(f"  <i>(Menampilkan 5 sesi terakhir dari total {total_hadir} kehadiran)</i>")
    else:
        history_lines.append("  <i>Belum ada riwayat presensi tercatat di semester ini.</i>")

    history_text = "\n".join(history_lines)

    text = (
        f"📚 <b>DETAIL MATA KULIAH (ETHOL PENS)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📖 <b>Mata Kuliah:</b> <b>{nama_mk}</b>\n"
        f"🔖 <b>Kelas:</b> <code>{kelas}</code>\n"
        f"🆔 <b>NIP Dosen:</b> <code>{nip}</code>\n"
        f"👨‍🏫 <b>Nama Dosen:</b> {dosen}\n"
        f"📅 <b>Jadwal:</b> {jadwal}\n"
        f"🏛 <b>Ruang:</b> {ruang}\n\n"
        f"🕒 <b>Presensi Terakhir Dosen:</b>\n"
        f"   {presensi_terakhir} ({status_presensi_badge})\n\n"
        f"📋 <b>Dokumen Perkuliahan:</b>\n"
        f"   • Kontrak Perkuliahan: {kontrak_status}\n"
        f"   • Rencana Pembelajaran (RPS): {rps_display}\n\n"
        f"📊 <b>History Presensi Saya ({total_hadir} Kehadiran):</b>\n"
        f"{history_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    # Keyboard aksi untuk detail ini
    keyboard: List[List[InlineKeyboardButton]] = []

    # Baris tombol Presensi
    if is_open:
        keyboard.append([InlineKeyboardButton("📝 Presensi Sekarang (Terbuka)", callback_data=f"absen_mk_{kuliah_id}")])
    else:
        keyboard.append([InlineKeyboardButton("🔴 Sesi Presensi Ditutup", callback_data="noop")])

    keyboard.append([
        InlineKeyboardButton("📚 Tugas MK Ini", callback_data=f"matkul_tasks_{kuliah_id}"),
        InlineKeyboardButton("🔙 Daftar Matkul", callback_data="btn_matkul"),
    ])

    markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.edit_text(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def cmd_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /detail <nama_atau_nomor_mk>: Menampilkan detail lengkap suatu mata kuliah."""
    message = update.effective_message
    if not message:
        return

    args = context.args or []
    if not args:
        await message.reply_text(
            "Format perintah: <code>/detail &lt;nomor_atau_nama_matkul&gt;</code>\n\n"
            "<i>Contoh:</i>\n"
            "• <code>/detail Basis Data Lanjut</code>\n"
            "• <code>/detail 11114</code>\n\n"
            "Atau gunakan perintah <code>/matkul</code> untuk memilih dari tombol interaktif.",
            parse_mode=ParseMode.HTML,
        )
        return

    query = " ".join(args).strip()
    await render_detail_matkul(update, context, query)


async def cmd_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /deadline: Menampilkan tugas pengguna dengan deadline mendesak (<= 3 hari)."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    client: EtholClient = context.bot_data["client"]
    db: DatabaseManager = context.bot_data["db"]

    is_auth, _, _ = await _get_authenticated_user_credentials(user.id, db, client)
    if not is_auth:
        await message.reply_text("⚠️ Anda belum login. Silakan gunakan /login terlebih dahulu.", parse_mode=ParseMode.HTML)
        return

    wait_msg = await message.reply_text("🔍 <i>Mencari tugas mendesak di akun ETHOL Anda...</i>", parse_mode=ParseMode.HTML)
    urgent_tasks = client.fetch_deadline_tugas(user_id=user.id, days_threshold=3)

    if not urgent_tasks:
        await wait_msg.edit_text(
            "🎉 <b>Tenang!</b> Tidak ada tugas kuliah dengan batas waktu mendesak (≤ 3 hari).\n"
            "Ketik <code>/tugas</code> untuk melihat daftar seluruh tugas aktif.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = ["🚨 <b>TUGAS DENGAN DEADLINE MENDEKAT (≤ 3 HARI):</b>\n━━━━━━━━━━━━━━━━━━━━━━━━"]
    for idx, t in enumerate(urgent_tasks, start=1):
        hours = t.get("hours_left", 0)
        if t.get("is_overdue"):
            badge = "⚠️ <b>SUDAH LEWAT DEADLINE!</b>"
        elif hours <= 12:
            badge = f"🔥 <b>Kritis: Sisa {hours} Jam!</b>"
        elif hours <= 24:
            badge = f"⚡ <b>Segera: Sisa {hours} Jam!</b>"
        else:
            days_left = round(hours / 24, 1)
            badge = f"⏳ Sisa ~{days_left} hari ({hours} jam)"

        line = (
            f"<b>{idx}. {html.escape(t.get('judul', '-'))}</b>\n"
            f"   📖 {html.escape(t.get('matkul', '-'))}\n"
            f"   👨‍🏫 {html.escape(t.get('dosen', '-'))}\n"
            f"   ⏰ Batas: <code>{html.escape(t.get('deadline', '-'))}</code>\n"
            f"   {badge}"
        )
        if t.get("link"):
            line += f" | <a href='{t['link']}'>Buka ETHOL</a>"
        lines.append(line)

    await wait_msg.edit_text("\n\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_materi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /materi: Menampilkan materi kuliah terbaru pengguna."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    client: EtholClient = context.bot_data["client"]
    db: DatabaseManager = context.bot_data["db"]

    is_auth, _, _ = await _get_authenticated_user_credentials(user.id, db, client)
    if not is_auth:
        await message.reply_text("⚠️ Anda belum login. Silakan gunakan /login terlebih dahulu.", parse_mode=ParseMode.HTML)
        return

    wait_msg = await message.reply_text("🔄 <i>Mengambil daftar materi perkuliahan...</i>", parse_mode=ParseMode.HTML)
    materials = client.fetch_materi(user.id)

    if not materials:
        await wait_msg.edit_text("ℹ️ Belum ada materi kuliah baru yang diunggah oleh dosen di ETHOL.", parse_mode=ParseMode.HTML)
        return

    lines = ["📎 <b>DAFTAR MATERI KULIAH TERBARU:</b>\n━━━━━━━━━━━━━━━━━━━━━━━━"]
    for idx, m in enumerate(materials[:10], start=1):
        line = (
            f"<b>{idx}. {html.escape(m.get('judul', '-'))}</b>\n"
            f"   📖 {html.escape(m.get('matkul', '-'))}\n"
            f"   👨‍🏫 Dosen: {html.escape(m.get('dosen', '-'))}\n"
            f"   📅 Waktu: {html.escape(m.get('tanggal', '-'))}"
        )
        if m.get("link"):
            line += f" | <a href='{m['link']}'>Unduh Materi</a>"
        lines.append(line)

    await wait_msg.edit_text("\n\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /filter [add|del|list|watchlist]: Mengelola filter kata kunci & watchlist MK pengguna di database."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    db: DatabaseManager = context.bot_data["db"]
    args = context.args or []

    if not args or args[0].lower() == "list":
        user_filters = db.get_user_filters(user.id)
        blacklist = ", ".join(f"<code>{html.escape(k)}</code>" for k in sorted(user_filters["exclude"])) or "<i>(Kosong)</i>"
        watchlist = ", ".join(f"<code>{html.escape(w)}</code>" for w in sorted(user_filters["watchlist"])) or "<i>(Kosong)</i>"
        muted = ", ".join(f"<code>{html.escape(m)}</code>" for m in sorted(user_filters["muted"])) or "<i>(Kosong)</i>"

        text = (
            f"⚙️ <b>PENGATURAN FILTER & WATCHLIST ANDA</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🚫 <b>Kata Kunci Ditolak (Blacklist):</b>\n{blacklist}\n\n"
            f"⭐ <b>Watchlist Mata Kuliah Prioritas:</b>\n{watchlist}\n\n"
            f"🔕 <b>Kategori Di-Mute:</b>\n{muted}\n\n"
            f"💡 <b>Panduan Perintah:</b>\n"
            f"• <code>/filter add &lt;kata&gt;</code> - Tambah blacklist\n"
            f"• <code>/filter del &lt;kata&gt;</code> - Hapus blacklist\n"
            f"• <code>/filter watchlist add &lt;nama_mk&gt;</code> - Tambah watchlist\n"
            f"• <code>/filter watchlist del &lt;nama_mk&gt;</code> - Hapus watchlist\n"
        )
        await message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    sub = args[0].lower()

    if sub == "add":
        if len(args) < 2:
            await message.reply_text("Format: <code>/filter add &lt;kata&gt;</code>", parse_mode=ParseMode.HTML)
            return
        kw = " ".join(args[1:])
        db.add_user_filter(user.id, "exclude", kw)
        await message.reply_text(f"✅ Kata kunci <code>{html.escape(kw)}</code> berhasil ditambahkan ke blacklist Anda.", parse_mode=ParseMode.HTML)

    elif sub == "del":
        if len(args) < 2:
            await message.reply_text("Format: <code>/filter del &lt;kata&gt;</code>", parse_mode=ParseMode.HTML)
            return
        kw = " ".join(args[1:])
        if db.remove_user_filter(user.id, "exclude", kw):
            await message.reply_text(f"✅ Kata kunci <code>{html.escape(kw)}</code> berhasil dihapus dari blacklist Anda.", parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(f"⚠️ Kata kunci <code>{html.escape(kw)}</code> tidak ditemukan dalam blacklist Anda.", parse_mode=ParseMode.HTML)

    elif sub == "watchlist":
        if len(args) < 3:
            await message.reply_text("Format: <code>/filter watchlist add|del &lt;nama_mk&gt;</code>", parse_mode=ParseMode.HTML)
            return
        action = args[1].lower()
        mk = " ".join(args[2:])
        if action == "add":
            db.add_user_filter(user.id, "watchlist", mk)
            await message.reply_text(f"⭐ Mata kuliah <code>{html.escape(mk)}</code> berhasil ditambahkan ke watchlist prioritas Anda.", parse_mode=ParseMode.HTML)
        elif action == "del":
            if db.remove_user_filter(user.id, "watchlist", mk):
                await message.reply_text(f"✅ Mata kuliah <code>{html.escape(mk)}</code> dihapus dari watchlist Anda.", parse_mode=ParseMode.HTML)
            else:
                await message.reply_text(f"⚠️ Mata kuliah <code>{html.escape(mk)}</code> tidak ditemukan dalam watchlist Anda.", parse_mode=ParseMode.HTML)


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /mute [kategori]: Menonaktifkan notifikasi kategori tertentu untuk pengguna."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    db: DatabaseManager = context.bot_data["db"]
    args = context.args or []
    categories = ["tugas", "materi", "presensi", "notifikasi"]

    user_filters = db.get_user_filters(user.id)
    muted_set = user_filters["muted"]

    if not args:
        keyboard = [
            [
                InlineKeyboardButton(f"{'🔕' if c in muted_set else '🔔'} {c.capitalize()}", callback_data=f"toggle_mute_{c}")
                for c in categories[:2]
            ],
            [
                InlineKeyboardButton(f"{'🔕' if c in muted_set else '🔔'} {c.capitalize()}", callback_data=f"toggle_mute_{c}")
                for c in categories[2:]
            ],
        ]
        await message.reply_text(
            "🔕 <b>PENGATURAN MUTE NOTIFIKASI ANDA:</b>\nKlik kategori di bawah untuk toggle mute/unmute:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML,
        )
        return

    cat = args[0].lower()
    if cat not in categories:
        await message.reply_text(f"Kategori tidak valid. Pilih dari: {', '.join(categories)}")
        return

    db.add_user_filter(user.id, "muted", cat)
    await message.reply_text(f"🔕 Notifikasi kategori <code>{html.escape(cat)}</code> berhasil di-mute.", parse_mode=ParseMode.HTML)


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /unmute [kategori]: Mengaktifkan kembali notifikasi kategori tertentu."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    db: DatabaseManager = context.bot_data["db"]
    args = context.args or []

    if not args:
        await message.reply_text("Format: <code>/unmute &lt;tugas|materi|presensi|notifikasi&gt;</code>", parse_mode=ParseMode.HTML)
        return

    cat = args[0].lower()
    if db.remove_user_filter(user.id, "muted", cat):
        await message.reply_text(f"🔔 Notifikasi kategori <code>{html.escape(cat)}</code> diaktifkan kembali.", parse_mode=ParseMode.HTML)
    else:
        await message.reply_text(f"ℹ️ Kategori <code>{html.escape(cat)}</code> saat ini memang tidak di-mute.", parse_mode=ParseMode.HTML)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /pause: Menjeda polling 30 detik untuk akun pengguna ini."""
    user = update.effective_user
    if not user:
        return

    db: DatabaseManager = context.bot_data["db"]
    db.set_user_paused(user.id, is_paused=True)

    if update.effective_message:
        await update.effective_message.reply_text(
            "⏸️ <b>Polling untuk akun Anda berhasil dijeda.</b>\n"
            "Bot tidak akan mengambil data/auto-absen hingga Anda mengirimkan /resume.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /resume: Melanjutkan kembali polling 30 detik untuk akun pengguna ini."""
    user = update.effective_user
    if not user:
        return

    db: DatabaseManager = context.bot_data["db"]
    db.set_user_paused(user.id, is_paused=False)

    if update.effective_message:
        await update.effective_message.reply_text(
            "▶️ <b>Polling untuk akun Anda aktif kembali.</b>\n"
            "Pemeriksaan notifikasi dan auto-absen akan berjalan setiap 30 detik.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /debug: Mengunduh dan menyimpan halaman HTML ke logs/debug_*.html untuk kalibrasi."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    client: EtholClient = context.bot_data["client"]
    db: DatabaseManager = context.bot_data["db"]

    await _get_authenticated_user_credentials(user.id, db, client)

    wait_msg = await message.reply_text(
        "🛠️ <i>Menjalankan inspect_mode()... Mengunduh respon API & CAS login untuk kalibrasi.</i>",
        parse_mode=ParseMode.HTML,
    )

    saved_items = client.inspect_mode(user_id=user.id)

    lines = [
        "🛠️ <b>HASIL INSPEKSI MODE DEBUG:</b>\n"
        "File berhasil disimpan di folder <code>logs/</code>:\n"
        "━━━━━━━━━━━━━━━━━━━━"
    ]

    for item in saved_items:
        page_name = item.get("page", "")
        filename = item.get("file", "")
        size_kb = item.get("size_kb", 0)
        status = "✅" if item.get("status") == "success" else "⚠️"
        lines.append(f"{status} <b>{page_name.upper()}:</b> <code>logs/{filename}</code> ({size_kb} KB)")

    await wait_msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /connect: Menghasilkan kode unik untuk menghubungkan akun ke WhatsApp (-connect <kode>)."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    db: DatabaseManager = context.bot_data["db"]
    user_data = db.get_or_create_user(user.id, user.username, user.first_name)

    if not user_data.get("is_logged_in"):
        await message.reply_text(
            "⚠️ <b>Akun belum login ke ETHOL PENS.</b>\n\n"
            "Silakan login terlebih dahulu menggunakan perintah:\n"
            "<code>/login &lt;nrp&gt; &lt;password&gt;</code>\n\n"
            "Setelah berhasil login, jalankan kembali /connect untuk mendapatkan kode tautan WhatsApp.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Buat kode unik baru (berlaku 10 menit)
    code = db.create_wa_connect_code(user.id, ttl_seconds=600)

    # Ambil tautan chat WhatsApp saat ini jika ada
    existing_links = db.get_user_whatsapp_links_detail(user.id)
    links_info = ""
    if existing_links:
        links_info = "\n<b>Perangkat/Chat WhatsApp Tertaut:</b>\n"
        for idx, l in enumerate(existing_links, 1):
            target = l["wa_chat_id"]
            ctype = "Grup" if l.get("chat_type") == "group" else "Pribadi"
            links_info += f"{idx}. <code>{target}</code> ({ctype})\n"

    msg = (
        "<b>Tautan WhatsApp</b>\n\n"
        f"Kode Unik: <code>{code}</code>\n"
        "Masa Berlaku: 10 Menit\n\n"
        "<b>Cara Menghubungkan:</b>\n"
        "Kirim perintah berikut di WhatsApp (chat pribadi bot atau grup yang dituju):\n"
        f"<code>-connect {code}</code>\n"
        f"{links_info}\n"
        "<i>Catatan: User dengan nomor yang sama dapat menautkan akun berbeda ke chat/grup masing-masing. "
        "WhatsApp hanya akan menerima pengumuman tugas, absensi, dan notifikasi tugas secara ringkas.</i>"
    )

    await message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler /disconnect: Memutus seluruh tautan nomor/grup WhatsApp untuk akun ini."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    db: DatabaseManager = context.bot_data["db"]
    count = db.unlink_user_whatsapp(user.id)
    if count > 0:
        await message.reply_text(
            f"✅ <b>Tautan WhatsApp Berhasil Diputus</b>\n"
            f"Sebanyak {count} nomor/grup WhatsApp telah diputus dari akun ETHOL Anda.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.reply_text(
            "ℹ️ <b>Tidak Ada Tautan WhatsApp</b>\n"
            "Akun ETHOL Anda saat ini belum menautkan nomor atau grup WhatsApp manapun.",
            parse_mode=ParseMode.HTML,
        )


# Alias untuk backwards compatibility
cmd_unconnect = cmd_disconnect


async def cmd_skem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/skem <nrp> untuk mendaftarkan bot mengawasi NRP.
    /skem check [nrp] untuk melihat profil & riwayat SKEM.
    /skem list untuk melihat daftar NRP yang diawasi.
    /skem stop <nrp> untuk menghentikan pengawasan.
    """
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    db: DatabaseManager = context.bot_data["db"]
    args = context.args or []

    if not args:
        await message.reply_text(
            "<b>📊 Layanan SKEM PENS (PJJ & Reguler)</b>\n\n"
            "<b>Perintah Tersedia:</b>\n"
            "  • <code>/skem &lt;NRP&gt;</code> → Daftarkan bot untuk terus mengawasi NRP ini\n"
            "  • <code>/skem check &lt;NRP&gt;</code> → Cek profil & riwayat ajuan SKEM saat ini\n"
            "  • <code>/skem list</code> → Daftar NRP yang sedang diawasi\n"
            "  • <code>/skem stop &lt;NRP&gt;</code> → Berhenti mengawasi NRP\n\n"
            "<i>Mendukung mahasiswa PJJ dan Reguler (Pencarian otomatis di kedua portal).</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    sub_cmd = args[0].lower()

    # 1. Perintah: /skem list
    if sub_cmd == "list":
        monitors = db.get_skem_monitors_for_user(user.id)
        if not monitors:
            await message.reply_text(
                "ℹ️ Belum ada NRP yang didaftarkan untuk diawasi bot.\n"
                "Gunakan: <code>/skem &lt;nrp&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
        else:
            lines = "\n".join([f"• <code>{m['nrp']}</code> (Portal: {m.get('portal', 'auto').upper()}, sejak {m['created_at'][:10]})" for m in monitors])
            await message.reply_text(
                f"<b>📊 Daftar NRP yang Sedang Diawasi:</b>\n{lines}\n\n"
                "Gunakan <code>/skem stop &lt;nrp&gt;</code> untuk berhenti mengawasi.",
                parse_mode=ParseMode.HTML,
            )
        return

    # 2. Perintah: /skem stop <nrp>
    if sub_cmd == "stop":
        if len(args) < 2:
            await message.reply_text("Gunakan: <code>/skem stop &lt;nrp&gt;</code>", parse_mode=ParseMode.HTML)
            return
        nrp = args[1].strip()
        removed = db.remove_skem_monitor(user.id, nrp)
        if removed > 0:
            await message.reply_text(f"✅ Pengawasan SKEM untuk NRP <code>{nrp}</code> telah dihentikan.", parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(f"ℹ️ NRP <code>{nrp}</code> tidak ditemukan dalam daftar pengawasan kamu.", parse_mode=ParseMode.HTML)
        return

    # 3. Perintah: /skem check [nrp] [portal] (Melihat profil SKEM)
    if sub_cmd == "check":
        target_nrp = ""
        portal_choice = "auto"

        if len(args) > 1 and args[1].strip().isdigit() and len(args[1].strip()) >= 6:
            target_nrp = args[1].strip()
            if len(args) > 2 and args[2].lower() in ("pjj", "reguler", "regular"):
                portal_choice = args[2].lower()
        else:
            # Ambil dari akun login atau monitor yang tersimpan
            user_data = db.get_user(user.id) or {}
            disp = user_data.get("ethol_username_display", "")
            if disp and disp.isdigit() and len(disp) >= 6:
                target_nrp = disp
            else:
                monitors = db.get_skem_monitors_for_user(user.id)
                if monitors:
                    target_nrp = monitors[0]["nrp"]

        if not target_nrp:
            await message.reply_text(
                "❌ Masukkan nomor NRP untuk melihat profil SKEM.\n"
                "Format: <code>/skem check &lt;NRP&gt; [pjj/reguler]</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        status_msg = await message.reply_text(
            f"⏳ <b>Sedang mengambil data profil SKEM untuk NRP <code>{target_nrp}</code> dari portal PENS...</b>",
            parse_mode=ParseMode.HTML,
        )

        try:
            from core.skem_monitor import fetch_skem_data, format_skem_profile
            data = await asyncio.to_thread(fetch_skem_data, target_nrp, portal_choice)

            if data:
                reply_html = format_skem_profile(data, is_html=True)
                reply_html += f"\n\n💡 <i>Gunakan <code>/skem {target_nrp}</code> jika ingin bot terus mengawasi ajuan baru NRP ini.</i>"
                await status_msg.edit_text(reply_html, parse_mode=ParseMode.HTML)
            else:
                await status_msg.edit_text(
                    f"ℹ️ <b>Data SKEM Belum Ditemukan</b>\n\n"
                    f"NRP <code>{target_nrp}</code> saat ini belum memiliki riwayat ajuan kegiatan SKEM di portal PENS (PJJ maupun Reguler).",
                    parse_mode=ParseMode.HTML,
                )
        except Exception as exc:
            logger.error("Error cmd_skem check: %s", exc)
            await status_msg.edit_text(f"❌ Terjadi kesalahan saat memeriksa data SKEM: {exc}", parse_mode=ParseMode.HTML)
        return

    # 4. Default: /skem <NRP> [portal] -> Mendaftarkan bot untuk terus mengawasi NRP tersebut
    nrp = args[0].strip()
    if not nrp.isdigit() or len(nrp) < 6:
        await message.reply_text(
            "❌ Format NRP tidak valid. Masukkan NRP berupa angka (minimal 6 digit).\n"
            "Format:\n"
            "  • <code>/skem &lt;NRP&gt;</code> (Untuk mengawasi NRP)\n"
            "  • <code>/skem check &lt;NRP&gt;</code> (Untuk melihat profil SKEM)",
            parse_mode=ParseMode.HTML,
        )
        return

    portal = "auto"
    if len(args) > 1 and args[1].lower() in ("pjj", "reguler", "regular"):
        portal = args[1].lower()

    # Daftarkan ke database monitoring
    db.upsert_skem_monitor(user.id, nrp, portal=portal)

    await message.reply_text(
        f"✅ <b>Pengawasan SKEM Berhasil Diaktifkan!</b>\n\n"
        f"<b>NRP:</b> <code>{nrp}</code>\n"
        f"Bot kini terus mengawasi portal SKEM PENS (<code>pens.id/skem-monitor-pjj</code>) di latar belakang.\n\n"
        f"Anda akan menerima notifikasi otomatis jika:\n"
        f"• Ada form ajuan baru yang terdeteksi\n"
        f"• Status ajuan berubah (Diterima / Ditolak)\n\n"
        f"<i>Ketik <code>/skem check {nrp}</code> jika ingin melihat profil & riwayat ajuan SKEM saat ini.</i>",
        parse_mode=ParseMode.HTML,
    )

