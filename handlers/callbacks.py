"""Handler interaksi inline keyboard callback query untuk ethol-bot (Multi-User)."""

from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from core.database import DatabaseManager
from core.ethol_client import EtholClient
from handlers.commands import (
    cmd_absen,
    cmd_connect,
    cmd_deadline,
    cmd_debug,
    cmd_filter,
    cmd_materi,
    cmd_matkul,
    cmd_mute,
    cmd_start,
    cmd_status,
    cmd_tugas,
    execute_manual_absen,
    get_main_menu_keyboard,
    render_absen_dashboard,
    render_detail_matkul,
)
from utils.logger import setup_logger

logger = setup_logger(__name__)


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Router utama untuk menangani semua interaksi inline keyboard multi-user."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    data = query.data or ""
    db: DatabaseManager = context.bot_data["db"]
    client: EtholClient = context.bot_data["client"]

    # Handler noop (tombol mati/info)
    if data == "noop":
        try:
            await query.answer("ℹ️ Sesi presensi untuk mata kuliah ini sedang ditutup oleh dosen pengampu.", show_alert=True)
        except Exception:
            pass
        return

    try:
        await query.answer()
    except Exception as exc:
        logger.debug("Gagal menjawab callback query (network blip): %s", exc)

    if data == "btn_status":
        await cmd_status(update, context)
    elif data == "btn_absen":
        dashboard_text, keyboard = render_absen_dashboard(user.id, db, client)
        if query.message:
            await query.message.edit_text(dashboard_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        else:
            await cmd_absen(update, context)
    elif data == "toggle_auto_absen":
        new_state = db.toggle_auto_absen(user.id)
        alert_msg = "Auto-absen berhasil diaktifkan 🟢" if new_state else "Auto-absen dinonaktifkan 🔴"
        await query.answer(alert_msg)
        dashboard_text, keyboard = render_absen_dashboard(user.id, db, client)
        if query.message:
            await query.message.edit_text(dashboard_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif data in ["btn_absen_refresh", "refresh_absen_status"]:
        await query.answer("Memperbarui status sesi presensi... 🔄")
        dashboard_text, keyboard = render_absen_dashboard(user.id, db, client)
        if query.message:
            await query.message.edit_text(dashboard_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif data == "btn_absen_manual_now":
        await query.answer("Menjalankan presensi manual... ⚡")
        _, report_text = await execute_manual_absen(user.id, client, db)
        back_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Dashboard Presensi", callback_data="btn_absen")],
            [InlineKeyboardButton("🔙 Menu Utama", callback_data="btn_menu_utama")],
        ])
        if query.message:
            await query.message.edit_text(report_text, reply_markup=back_keyboard, parse_mode=ParseMode.HTML)
    elif data == "btn_absen_history":
        await query.answer("Memuat riwayat presensi... 📋")
        logs = db.get_recent_presensi_logs(user.id, limit=10)
        lines = [
            "📋 <b>RIWAYAT PRESENSI BOT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Berikut 10 aktivitas presensi terakhir yang tercatat:"
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
        if query.message:
            await query.message.edit_text("\n\n".join(lines), reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif data == "btn_tugas":
        await cmd_tugas(update, context)
    elif data == "btn_deadline":
        await cmd_deadline(update, context)
    elif data == "btn_materi":
        await cmd_materi(update, context)
    elif data == "btn_matkul":
        await cmd_matkul(update, context)
    elif data == "btn_menu_utama":
        if query.message:
            await query.message.edit_text(
                "🤖 <b>ETHOL PENS Telegram Assistant</b>\n"
                "Pilih menu di bawah ini untuk mengakses fitur akademik Anda:",
                reply_markup=get_main_menu_keyboard(),
                parse_mode=ParseMode.HTML,
            )
    elif data == "btn_filter_info":
        await cmd_filter(update, context)
    elif data == "btn_connect_wa":
        await cmd_connect(update, context)
    elif data == "btn_mute_menu":
        await cmd_mute(update, context)
    elif data == "btn_debug":
        await cmd_debug(update, context)
    elif data == "btn_pause_toggle":
        user_data = db.get_user(user.id)
        is_paused = bool(user_data.get("is_paused", 0)) if user_data else False
        new_state = not is_paused
        db.set_user_paused(user.id, new_state)

        text = "⏸️ <b>Polling untuk akun Anda dijeda sementara.</b>" if new_state else "▶️ <b>Polling untuk akun Anda dilanjutkan kembali.</b>"
        if query.message:
            await query.message.reply_text(text, parse_mode=ParseMode.HTML)

    # 1. Navigasi Detail Mata Kuliah: detail_mk_<kuliah_id>
    elif data.startswith("detail_mk_"):
        mk_id_str = data.replace("detail_mk_", "").strip()
        try:
            mk_id = int(mk_id_str)
        except ValueError:
            mk_id = mk_id_str
        await render_detail_matkul(update, context, mk_id)

    # 2. Presensi Langsung dari Halaman Detail: absen_mk_<kuliah_id>
    elif data.startswith("absen_mk_"):
        mk_id_str = data.replace("absen_mk_", "").strip()
        try:
            mk_id = int(mk_id_str)
        except ValueError:
            mk_id = mk_id_str

        detail = client.get_detail_matkul(user.id, mk_id)
        if not detail:
            await query.answer("Data mata kuliah tidak ditemukan.", show_alert=True)
            return

        c = detail["course"]
        mk_name = c.get("nama", "Mata Kuliah")
        active_sess = detail.get("active_session")
        presensi_key = detail.get("presensi_key")

        if not detail.get("is_presensi_open") or not presensi_key:
            await query.answer("Sesi presensi sudah ditutup atau belum dibuka oleh dosen.", show_alert=True)
            return

        sess_item = {
            "mk_code": str(c.get("nomor")),
            "mk_name": mk_name,
            "kuliah": c.get("nomor"),
            "jenis_schema": c.get("jenis_schema", 2),
            "kuliah_asal": c.get("kuliah_asal"),
            "key": presensi_key,
        }

        res = client.submit_absen(user.id, sess_item)
        success = res.get("success", False)
        already_attended = res.get("already_attended", False)
        icon = "✅" if (success or already_attended) else "❌"
        msg = res.get("message", "")

        status_log = "MANUAL_SUCCESS" if (success and not already_attended) else ("ALREADY" if already_attended else "MANUAL_FAILED")
        db.log_presensi(user.id, str(c.get("nomor")), mk_name, presensi_key, status_log, msg)

        if success or already_attended:
            db.set_presensi_cooldown(user.id, str(c.get("nomor")), datetime.now().timestamp() + 43200)

        await query.answer(f"{icon} {msg}", show_alert=True)

        # Muat ulang tampilan detail setelah absen
        await render_detail_matkul(update, context, mk_id)

    # 3. Lihat Tugas MK Spesifik: matkul_tasks_<kuliah_id>
    elif data.startswith("matkul_tasks_"):
        mk_id_str = data.replace("matkul_tasks_", "").strip()
        try:
            mk_id = int(mk_id_str)
        except ValueError:
            mk_id = 0

        tasks = client.fetch_tugas(user.id, include_all=True, target_kuliah=mk_id)
        if not tasks:
            await query.answer("Tidak ada tugas pada mata kuliah ini.", show_alert=True)
            return

        lines = [f"📚 <b>DAFTAR TUGAS UNTUK {html.escape(tasks[0].get('matkul', 'MATA KULIAH'))}:</b>\n━━━━━━━━━━━━━━━━━━━━"]
        for idx, t in enumerate(tasks, start=1):
            is_sub = t.get("is_submitted")
            status_line = f"🟢 Sudah Dikumpulkan ({t.get('submission_time', '-')})" if is_sub else "🔴 Belum Dikumpulkan"
            lines.append(
                f"<b>{idx}. {html.escape(t.get('judul', '-'))}</b>\n"
                f"   ⏰ Deadline: <code>{html.escape(t.get('deadline', '-'))}</code>\n"
                f"   📌 Status: {status_line}"
            )

        lines.append(f"\n💡 <i>Klik Kembali untuk melihat detail mata kuliah.</i>")
        keyboard = [
            [InlineKeyboardButton("🔙 Kembali ke Detail MK", callback_data=f"detail_mk_{mk_id}")],
            [InlineKeyboardButton("📖 Daftar Mata Kuliah", callback_data="btn_matkul")],
        ]

        if query.message:
            await query.message.edit_text(
                "\n\n".join(lines),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML,
            )

    # 4. Toggle Mute per Kategori
    elif data.startswith("toggle_mute_"):
        cat = data.replace("toggle_mute_", "").strip().lower()
        is_now_muted = db.toggle_user_mute(user.id, cat)

        status_str = "🔕 di-mute" if is_now_muted else "🔔 di-unmute"
        user_filters = db.get_user_filters(user.id)
        muted_set = user_filters["muted"]

        categories = ["tugas", "materi", "presensi", "notifikasi"]
        keyboard = [
            [
                InlineKeyboardButton(
                    f"{'🔕' if c in muted_set else '🔔'} {c.capitalize()}",
                    callback_data=f"toggle_mute_{c}",
                )
                for c in categories[:2]
            ],
            [
                InlineKeyboardButton(
                    f"{'🔕' if c in muted_set else '🔔'} {c.capitalize()}",
                    callback_data=f"toggle_mute_{c}",
                )
                for c in categories[2:]
            ],
            [
                InlineKeyboardButton("🔙 Menu Utama", callback_data="btn_menu_utama")
            ]
        ]

        if query.message:
            await query.message.edit_text(
                f"🔕 <b>PENGATURAN MUTE NOTIFIKASI ANDA:</b>\n"
                f"Status kategori <code>{html.escape(cat)}</code>: <b>{status_str}</b>\n\n"
                f"Klik kategori untuk mengubah:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML,
            )
