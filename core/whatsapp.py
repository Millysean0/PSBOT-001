"""Modul WhatsApp Client untuk notifikasi & announcement PSBOT-001.

Mendukung backend Baileys (Node.js via wa_bridge.js) yang ringan, cepat,
dan kompatibel 100% dengan Windows, Linux, dan Android (Termux).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from core.database import DatabaseManager
from utils.logger import setup_logger

logger = setup_logger(__name__)


def mask_account(identifier: str) -> str:
    """Menyensor alamat email atau NRP akun ETHOL untuk privasi di WhatsApp.

    Contoh:
        ahmad.fauzi@it.student.pens.ac.id -> ah****zi@it.student.pens.ac.id
        3125510021 -> 312****21
    """
    if not identifier:
        return "Pengguna"

    text = str(identifier).strip()

    if "@" in text:
        def _mask_local(match: re.Match) -> str:
            local = match.group(1)
            domain = match.group(2)
            if len(local) <= 2:
                masked = local[0] + "***"
            elif len(local) <= 4:
                masked = local[0] + "***" + local[-1]
            else:
                masked = local[:2] + "****" + local[-2:]
            return f"{masked}@{domain}"

        return re.sub(r"([a-zA-Z0-9_.+-]+)@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", _mask_local, text)

    if text.isdigit() and len(text) >= 6:
        return text[:3] + "****" + text[-2:]

    if len(text) > 4:
        return text[:2] + "****" + text[-2:]

    return text


NODE_EXECUTABLE = shutil.which("node")


class WhatsAppManager:
    """Pengelola koneksi dan pengiriman pesan WhatsApp untuk PSBOT-001."""

    def __init__(
        self,
        db: DatabaseManager,
        session_path: str = "wa_session",
        phone_number: Optional[str] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        tg_notifier: Optional[Any] = None,
        admin_user_id: Optional[int] = None,
    ) -> None:
        self.db = db
        self.session_path = session_path
        self.phone_number = phone_number.strip() if phone_number else None
        self.loop = loop
        self.tg_notifier = tg_notifier
        self.admin_user_id = admin_user_id

        self._is_running: bool = False
        self._is_connected: bool = False
        self.backend: Optional[str] = "baileys" if NODE_EXECUTABLE and os.path.exists("wa_bridge.js") else None

        self._node_proc: Optional[subprocess.Popen] = None
        self._node_reader_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def is_logged_in(self) -> bool:
        return self._is_connected

    def start(self) -> None:
        """Memulai background WhatsApp worker."""
        if self._is_running:
            return

        if self.backend == "baileys":
            self._start_baileys()
        else:
            logger.warning(
                "Node.js tidak ditemukan atau wa_bridge.js belum siap. "
                "Untuk mengaktifkan WhatsApp di Termux, jalankan: pkg install nodejs && npm install"
            )

    def stop(self) -> None:
        """Menghentikan worker WhatsApp."""
        self._is_running = False
        self._is_connected = False

        if self._node_proc:
            try:
                self._node_proc.terminate()
            except Exception:
                pass
            self._node_proc = None

        logger.info("WhatsAppManager berhasil dihentikan.")

    def _start_baileys(self) -> None:
        logger.info("Memulai WhatsApp backend: Baileys (Node.js)...")
        self._is_running = True

        env = os.environ.copy()
        env["WA_SESSION_DIR"] = self.session_path
        if self.phone_number:
            env["WA_PHONE_NUMBER"] = self.phone_number

        try:
            self._node_proc = subprocess.Popen(
                [NODE_EXECUTABLE, "wa_bridge.js"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                bufsize=1,
                env=env,
            )

            self._node_reader_thread = threading.Thread(
                target=self._baileys_stdout_loop,
                name="BaileysReader",
                daemon=True,
            )
            self._node_reader_thread.start()
            logger.info("Worker Baileys WhatsApp berhasil dijalankan.")
        except Exception as exc:
            logger.error("Gagal menjalankan node wa_bridge.js: %s", exc)

    def _baileys_stdout_loop(self) -> None:
        """Membaca event JSON dari wa_bridge.js."""
        while self._is_running and self._node_proc and self._node_proc.poll() is None:
            try:
                line = self._node_proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue

                data = json.loads(line)
                event_type = data.get("event")

                if event_type == "connected":
                    self._is_connected = True
                    logger.info("✅ WhatsApp (Baileys) terhubung ke server WhatsApp.")
                elif event_type == "disconnected":
                    self._is_connected = False
                    logger.warning("WhatsApp (Baileys) terputus.")
                elif event_type == "message":
                    chat_jid = data.get("chat_jid", "")
                    is_group = bool(data.get("is_group", False))
                    text = data.get("text", "")
                    self._handle_incoming_text(chat_jid, is_group, text)

            except Exception as exc:
                logger.debug("Error membaca event Baileys: %s", exc)

        self._is_connected = False

    def _handle_incoming_text(self, chat_jid: str, is_group: bool, text: str) -> None:
        """Memproses teks perintah masuk dari WhatsApp."""
        try:
            match_connect = re.match(r"^[-/]?connect\s+([A-Za-z0-9\-]+)", text, re.IGNORECASE)
            if match_connect:
                raw_code = match_connect.group(1).strip()
                self._process_connect_command(chat_jid, is_group, raw_code)
                return

            if re.match(r"^[-/]?disconnect\b", text, re.IGNORECASE):
                self._process_disconnect_command(chat_jid)
                return

            if re.match(r"^[-/]?status\b", text, re.IGNORECASE):
                self._process_status_command(chat_jid)
                return

            if re.match(r"^[-/]?testnotif\b", text, re.IGNORECASE):
                self._process_testnotif_command(chat_jid)
                return

            match_check = (
                re.match(r"^[-/]?skem\s*check(?:\s+([0-9]+))?", text, re.IGNORECASE)
                or re.match(r"^[-/]?skemcheck(?:\s+([0-9]+))?", text, re.IGNORECASE)
            )
            if match_check:
                nrp_target = (match_check.group(1) or "").strip()
                self._process_skem_check_command(chat_jid, nrp_target)
                return

            match_monitor = re.match(r"^[-/]?skem(?:\s+([0-9]+))?", text, re.IGNORECASE)
            if match_monitor:
                nrp_target = (match_monitor.group(1) or "").strip()
                self._process_skem_monitor_command(chat_jid, nrp_target)
                return

        except Exception as exc:
            logger.error("Error saat memproses perintah WhatsApp: %s", exc)

    def _process_connect_command(self, chat_jid: str, is_group: bool, code: str) -> None:
        user_id = self.db.validate_and_consume_wa_connect_code(code)
        if user_id:
            user_data = self.db.get_user(user_id) or {}
            nama = user_data.get("nama") or user_data.get("first_name") or f"User {user_id}"
            raw_display = user_data.get("ethol_username_display") or nama
            display_name = mask_account(raw_display)
            chat_type = "group" if is_group else "private"
            self.db.link_user_whatsapp(user_id, chat_jid, chat_type=chat_type)

            reply = (
                "✅ *Tautan Berhasil!*\n\n"
                f"Halo, *{nama}*! 👋\n"
                f"Chat ini ({('grup' if is_group else 'pribadi')}) sudah terhubung ke akunmu.\n\n"
                "Mulai sekarang kamu akan dapat notifikasi otomatis:\n"
                "• 📋 Tugas & deadline baru\n"
                "• 📚 Materi kuliah terbaru\n"
                "• ✅ Konfirmasi auto-absen\n"
                "• 🏅 Update status SKEM\n\n"
                "Ketik *-status* untuk melihat info tautan ini."
            )
            self._send_reply_sync(chat_jid, reply)
            logger.info("WhatsApp chat %s berhasil ditautkan ke user_id %s", chat_jid, user_id)

            if self.tg_notifier and self.loop:
                tg_text = (
                    "<b>✅ Tautan WhatsApp Berhasil!</b>\n\n"
                    f"Chat ID: <code>{chat_jid}</code>\n"
                    f"Tipe: {'Grup' if is_group else 'Pribadi'}\n\n"
                    "Akun kamu sekarang akan menerima notifikasi tugas, materi, absensi, dan SKEM di chat tersebut."
                )
                try:
                    asyncio.run_coroutine_threadsafe(
                        self.tg_notifier.send_text(chat_id=user_id, text=tg_text),
                        self.loop,
                    )
                except Exception:
                    pass
        else:
            reply = (
                "❌ *Tautan Gagal*\n\n"
                "Kode yang kamu masukkan tidak valid atau sudah kedaluwarsa.\n\n"
                "Coba lagi:\n"
                "1. Buka bot Telegram kamu\n"
                "2. Ketik /connect untuk mendapatkan kode baru\n"
                "3. Kirim kode tersebut ke sini dalam waktu 10 menit"
            )
            self._send_reply_sync(chat_jid, reply)

    def _process_disconnect_command(self, chat_jid: str) -> None:
        rows = self.db.unlink_by_wa_chat_id(chat_jid)
        if rows > 0:
            reply = (
                "🔌 *Tautan Diputus*\n\n"
                "Chat ini tidak lagi menerima notifikasi dari bot.\n"
                "Ketik *-connect <kode>* jika ingin terhubung lagi."
            )
        else:
            reply = (
                "ℹ️ *Belum Ada Tautan*\n\n"
                "Chat ini belum pernah ditautkan ke akun ETHOL manapun.\n"
                "Gunakan *-connect <kode>* untuk menautkan akun."
            )
        self._send_reply_sync(chat_jid, reply)

    def _process_status_command(self, chat_jid: str) -> None:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT u.ethol_username_display, u.nama, u.first_name, l.chat_type
                FROM user_whatsapp_links l
                JOIN users u ON l.user_id = u.user_id
                WHERE l.wa_chat_id = ?;
                """,
                (chat_jid,),
            ).fetchall()

        now_str = datetime.now().strftime("%d %b %Y, %H:%M WIB")
        if rows:
            chat_type_label = "Grup" if rows[0]["chat_type"] == "group" else "Pribadi"
            account_lines = []
            for r in rows:
                nama_display = r["nama"] or r["first_name"] or "Mahasiswa"
                masked = mask_account(r["ethol_username_display"] or nama_display)
                account_lines.append(f"  • {nama_display} ({masked})")
            accounts_str = "\n".join(account_lines)
            reply = (
                "📊 *Status Tautan WhatsApp*\n\n"
                f"💬 Tipe Chat: {chat_type_label}\n"
                f"🕐 Waktu: {now_str}\n\n"
                f"👤 Akun Tertaut:\n{accounts_str}\n\n"
                "Fitur aktif: notifikasi tugas, materi, absensi, SKEM ✅"
            )
        else:
            reply = (
                "📊 *Status Tautan WhatsApp*\n\n"
                "❌ Chat ini belum terhubung ke akun ETHOL manapun.\n\n"
                "Cara menautkan:\n"
                "1. Buka bot Telegram kamu → ketik /connect\n"
                "2. Salin kode yang diberikan\n"
                "3. Kirim ke sini: *-connect <kode>*"
            )
        self._send_reply_sync(chat_jid, reply)

    def _process_testnotif_command(self, chat_jid: str) -> None:
        with self.db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT u.ethol_username_display, u.nama, u.first_name, l.chat_type
                FROM user_whatsapp_links l
                JOIN users u ON l.user_id = u.user_id
                WHERE l.wa_chat_id = ?;
                """,
                (chat_jid,),
            ).fetchall()

        now_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
        if rows:
            nama_display = rows[0]["nama"] or rows[0]["first_name"] or "Mahasiswa"
            reply = (
                "🔔 *Test Notifikasi*\n\n"
                f"Halo, *{nama_display}*! 👋\n"
                "Jalur pengiriman WhatsApp kamu berfungsi normal ✅\n\n"
                f"🕐 Waktu: {now_str}\n"
                "Kamu akan menerima notifikasi otomatis dari PSBOT di sini."
            )
        else:
            reply = (
                "🔔 *Test Notifikasi*\n\n"
                "✅ Klien WhatsApp bot aktif dan berjalan.\n\n"
                f"🕐 Waktu: {now_str}\n\n"
                "⚠️ Chat ini belum tertaut ke akun manapun.\n"
                "Gunakan *-connect <kode>* untuk menautkan."
            )
        self._send_reply_sync(chat_jid, reply)

    def _resolve_nrp_for_chat(self, chat_jid: str, provided_nrp: str) -> tuple[str, int | None]:
        """Mencari NRP target dan user_id dari chat_jid menggunakan db.get_user_nrp().

        Returns:
            (nrp, user_id) — nrp kosong jika tidak ditemukan.
        """
        target_nrp = provided_nrp.strip() if provided_nrp else ""
        user_id: int | None = None

        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT user_id FROM user_whatsapp_links WHERE wa_chat_id = ? LIMIT 1;",
                (chat_jid,),
            ).fetchone()
            if row:
                user_id = row["user_id"]

        if not target_nrp and user_id:
            try:
                detected = self.db.get_user_nrp(user_id)
                if detected and detected.isdigit() and len(detected) >= 6:
                    target_nrp = detected
            except Exception:
                pass

        return target_nrp, user_id

    def _process_skem_check_command(self, chat_jid: str, nrp: str) -> None:
        """Memproses perintah -skemcheck di WhatsApp untuk melihat profil & riwayat SKEM."""
        target_nrp, _ = self._resolve_nrp_for_chat(chat_jid, nrp)

        if not target_nrp or not target_nrp.isdigit() or len(target_nrp) < 6:
            reply = (
                "📊 *Cek SKEM via WhatsApp*\n\n"
                "Format: *-skemcheck <NRP>*\n\n"
                "Contoh: -skemcheck 3125510021\n\n"
                "Bot akan mengambil data profil & riwayat kegiatan SKEM dari portal PENS."
            )
            self._send_reply_sync(chat_jid, reply)
            return

        self._send_reply_sync(chat_jid, f"⏳ Sedang ambil data SKEM untuk NRP *{target_nrp}*...\n_Mohon tunggu sebentar ya._")

        def _fetch_worker() -> None:
            try:
                from core.skem_monitor import fetch_skem_data, format_skem_profile
                data = fetch_skem_data(target_nrp)
                if data:
                    res_text = format_skem_profile(data, is_html=False)
                    res_text += f"\n\n💡 _Ketik -skem {target_nrp} agar bot pantau otomatis NRP ini._"
                    self._send_reply_sync(chat_jid, res_text)
                else:
                    self._send_reply_sync(
                        chat_jid,
                        f"ℹ️ *Data SKEM Belum Ditemukan*\n\n"
                        f"NRP *{target_nrp}* belum punya riwayat ajuan SKEM di portal PENS.\n"
                        "Coba lagi nanti atau periksa kembali NRP-nya."
                    )
            except Exception as e:
                logger.error("Error worker _process_skem_check_command: %s", e)
                self._send_reply_sync(chat_jid, f"❌ Gagal mengambil data SKEM: {e}")

        threading.Thread(target=_fetch_worker, daemon=True).start()

    def _process_skem_monitor_command(self, chat_jid: str, nrp: str) -> None:
        """Memproses perintah -skem <nrp> di WhatsApp untuk mendaftarkan bot mengawasi NRP."""
        target_nrp, user_id = self._resolve_nrp_for_chat(chat_jid, nrp)

        if not target_nrp or not target_nrp.isdigit() or len(target_nrp) < 6:
            reply = (
                "📊 *Monitoring SKEM via WhatsApp*\n\n"
                "Format: *-skem <NRP>*\n\n"
                "Contoh: -skem 3125510021\n\n"
                "Bot akan pantau NRP tersebut setiap ~5 menit dan kirim notif jika ada perubahan.\n"
                "Ketik *-skemcheck <NRP>* untuk lihat profil SKEM sekarang."
            )
            self._send_reply_sync(chat_jid, reply)
            return

        if user_id:
            self.db.upsert_skem_monitor(user_id, target_nrp)
        else:
            pseudo_id = abs(hash(chat_jid)) % 1000000000
            self.db.upsert_skem_monitor(pseudo_id, target_nrp)

        reply = (
            "✅ *Pengawasan SKEM Aktif!*\n\n"
            f"*NRP:* {target_nrp}\n\n"
            "Bot sekarang terus pantau portal SKEM PENS di latar belakang.\n\n"
            "Kamu akan dapat notifikasi otomatis kalau:\n"
            "• 📄 Ada form ajuan baru\n"
            "• ✅ Ajuan diterima\n"
            "• ❌ Ajuan ditolak\n\n"
            f"_Ketik -skemcheck {target_nrp} untuk lihat profil SKEM saat ini._"
        )
        self._send_reply_sync(chat_jid, reply)

    def _send_reply_sync(self, to: str, message: str) -> bool:
        """Mengirim balasan langsung secara sinkronus."""
        if self._node_proc and self._node_proc.stdin:
            try:
                payload = json.dumps({"action": "send", "chat_id": to, "message": message}) + "\n"
                with self._lock:
                    self._node_proc.stdin.write(payload)
                    self._node_proc.stdin.flush()
                return True
            except Exception as exc:
                logger.error("Gagal kirim via Baileys stdin: %s", exc)
                return False
        return False

    async def send_announcement(self, wa_chat_id: str, message: str) -> bool:
        """Mengirim announcement asinkronus ke wa_chat_id."""
        if not self._is_connected:
            return False
        return await asyncio.to_thread(self._send_reply_sync, wa_chat_id, message)

    async def send_file(
        self,
        wa_chat_id: str,
        file_bytes: bytes,
        filename: str,
        caption: str = "",
    ) -> bool:
        """Mengirim file/dokumen asinkronus ke wa_chat_id via WhatsApp."""
        if not self._is_connected or not self._node_proc or not self._node_proc.stdin:
            return False

        try:
            import base64
            import mimetypes

            file_base64 = base64.b64encode(file_bytes).decode("utf-8")
            mimetype, _ = mimetypes.guess_type(filename)
            if not mimetype:
                mimetype = "application/octet-stream"

            payload = json.dumps({
                "action": "send_file",
                "chat_id": wa_chat_id,
                "file_base64": file_base64,
                "filename": filename,
                "mimetype": mimetype,
                "caption": caption,
            }) + "\n"

            def _write() -> bool:
                with self._lock:
                    self._node_proc.stdin.write(payload)
                    self._node_proc.stdin.flush()
                return True

            return await asyncio.to_thread(_write)
        except Exception as exc:
            logger.error("Gagal kirim file via Baileys stdin: %s", exc)
            return False
