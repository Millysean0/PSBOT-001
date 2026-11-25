"""Modul Notifier untuk memformat dan mengirim pesan ke Telegram dan WhatsApp.

Format pesan dibuat rapi, humanis, dan mudah dibaca sesuai kebutuhan mahasiswa:
- Absen dibuka: Menampilkan matkul, jam dibuka, dan minggu ke berapa.
  Sertakan @all hanya jika notifikasi dikirim ke grup WhatsApp (bukan di chat pribadi).
- Materi baru: Menampilkan matkul dan judul materi, serta otomatis mengunduh
  dan mengirim file materi langsung ke Telegram dan WhatsApp.
- Tugas baru: Menampilkan matkul, judul, deskripsi, dan otomatis mengunduh
  dan mengirim file lampiran langsung ke Telegram dan WhatsApp.
- Tugas dikumpulkan: Notifikasi tugas yang baru dikumpulkan beserta auto-download file jawaban.
- SKEM monitor: Menampilkan detail ajuan diterima / ditolak dengan format humanis.
"""

from __future__ import annotations

import html
import io
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from utils.logger import setup_logger

logger = setup_logger(__name__)


class Notifier:
    """Mengelola pengiriman pesan dan notifikasi ke Telegram dan WhatsApp."""

    def __init__(
        self,
        bot: Bot,
        db: Optional[Any] = None,
        wa_client: Optional[Any] = None,
        ethol_client: Optional[Any] = None,
        config_path: str = "config.json",
        discord_client: Optional[Any] = None,
    ) -> None:
        """Inisialisasi Notifier."""
        self.bot = bot
        self.db = db
        self.wa_client = wa_client
        self.ethol_client = ethol_client
        self.config_path = config_path
        self.discord_client = discord_client
        self.global_sent_count: int = 0
        self._load_features()

    def _load_features(self) -> None:
        """Memuat preferensi fitur & jenis notifikasi aktif dari config.json."""
        self.features: Dict[str, Any] = {
            "whatsapp_sync_enabled": True,
            "discord_sync_enabled": True,
            "auto_download_materi": True,
            "auto_download_tugas": True,
            "auto_download_submitted_task": True,
        }
        self.notifications_cfg: Dict[str, bool] = {
            "absen": True,
            "tugas": True,
            "tugas_dikumpulkan": True,
            "materi": True,
            "notifikasi_umum": True,
            "skem": True,
        }
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    self.features.update(cfg.get("features", {}))
                    notif_section = cfg.get("notifications", {})
                    if isinstance(notif_section, dict):
                        for k, v in notif_section.items():
                            self.notifications_cfg[str(k).lower()] = bool(v)
        except Exception as exc:
            logger.warning("Gagal memuat config features/notifications di Notifier: %s", exc)

    def is_notif_enabled(self, notif_type: str) -> bool:
        """Memeriksa apakah jenis notifikasi tertentu diaktifkan di config.json."""
        key = str(notif_type).lower().strip()
        alias_map = {
            "presensi": "absen",
            "absen": "absen",
            "tugas": "tugas",
            "tugas_dikumpulkan": "tugas_dikumpulkan",
            "materi": "materi",
            "notifikasi": "notifikasi_umum",
            "notifikasi_umum": "notifikasi_umum",
            "skem": "skem",
        }
        cfg_key = alias_map.get(key, key)
        return bool(self.notifications_cfg.get(cfg_key, True))

    @staticmethod
    def split_message(text: str, max_length: int = 4000) -> List[str]:
        """Memecah teks panjang menjadi beberapa bagian agar tidak melebihi batas pesan Telegram."""
        if len(text) <= max_length:
            return [text]

        chunks: List[str] = []
        lines = text.split("\n")
        current_chunk = ""

        for line in lines:
            if len(current_chunk) + len(line) + 1 > max_length:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                while len(line) > max_length:
                    chunks.append(line[:max_length])
                    line = line[max_length:]
                current_chunk = line + "\n"
            else:
                current_chunk += line + "\n"

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks

    async def send_text(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = ParseMode.HTML,
        disable_web_page_preview: bool = True,
    ) -> bool:
        """Mengirim pesan teks ke chat_id target Telegram."""
        chunks = self.split_message(text)
        success = True

        for chunk in chunks:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview,
                )
                self.global_sent_count += 1
            except TelegramError as exc:
                logger.error("Gagal mengirim pesan Telegram ke %s: %s", chat_id, exc)
                try:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        disable_web_page_preview=disable_web_page_preview,
                    )
                    self.global_sent_count += 1
                except Exception as inner_exc:
                    logger.error("Fallback plain text Telegram gagal ke %s: %s", chat_id, inner_exc)
                    success = False

        return success

    async def send_document(
        self,
        chat_id: int,
        file_bytes: bytes,
        filename: str,
        caption: str = "",
    ) -> bool:
        """Mengirim file/dokumen ke Telegram chat_id."""
        try:
            await self.bot.send_document(
                chat_id=chat_id,
                document=io.BytesIO(file_bytes),
                filename=filename,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            self.global_sent_count += 1
            return True
        except TelegramError as exc:
            logger.error("Gagal mengirim dokumen Telegram ke %s (%s): %s", chat_id, filename, exc)
            return False

    async def _send_to_whatsapp(self, user_id: int, message: str, is_absen: bool = False) -> None:
        """Mengirim pesan announcement ke semua WhatsApp chat/grup yang tertaut ke user_id.

        Aturan @all:
        - Jika is_absen=True dan target adalah GRUP (@g.us), tambahkan @all.
        - Jika target adalah CHAT PRIBADI (@s.whatsapp.net), jangan ada @all.
        """
        if not self.features.get("whatsapp_sync_enabled", True):
            return

        if not self.db or not self.wa_client:
            return

        wa_chats = self.db.get_user_whatsapp_chats(user_id)
        if not wa_chats:
            return

        for chat in wa_chats:
            chat_str = str(chat).strip()
            is_group = chat_str.endswith("@g.us")

            if is_absen:
                if is_group:
                    msg_to_send = message.rstrip()
                    if "@all" not in msg_to_send:
                        msg_to_send = f"{msg_to_send}\n\n@all"
                else:
                    msg_to_send = message.replace("@all", "").strip()
            else:
                msg_to_send = message.replace("@all", "").strip() if not is_group else message

            try:
                await self.wa_client.send_announcement(chat_str, msg_to_send)
            except Exception as exc:
                logger.error("Gagal broadcast WhatsApp ke %s: %s", chat_str, exc)

    async def _send_file_to_whatsapp(
        self,
        user_id: int,
        file_bytes: bytes,
        filename: str,
        caption: str = "",
    ) -> None:
        """Mengirim file/dokumen ke semua WhatsApp chat/grup yang tertaut ke user_id."""
        if not self.features.get("whatsapp_sync_enabled", True):
            return

        if not self.db or not self.wa_client:
            return

        wa_chats = self.db.get_user_whatsapp_chats(user_id)
        if not wa_chats:
            return

        for chat in wa_chats:
            try:
                await self.wa_client.send_file(chat, file_bytes, filename, caption)
            except Exception as exc:
                logger.error("Gagal kirim file WhatsApp ke %s: %s", chat, exc)

    async def _send_to_discord(
        self,
        user_id: int,
        message: str,
        embed: Optional[Any] = None,
        is_absen: bool = False,
    ) -> None:
        """Mengirim pesan announcement atau embed ke channel/DM Discord yang tertaut."""
        if not self.features.get("discord_sync_enabled", True):
            return

        if not self.discord_client or not self.discord_client.is_connected:
            return

        targets: List[str] = []
        if self.db:
            linked = self.db.get_user_discord_targets(user_id)
            targets = [str(item["discord_target_id"]) for item in linked if item.get("discord_target_id")]

        # Jika tidak ada target spesifik tapi ada default_channel_id global
        if not targets and getattr(self.discord_client, "default_channel_id", None):
            targets.append(str(self.discord_client.default_channel_id))

        if not targets:
            return

        for tid in targets:
            try:
                await self.discord_client.send_announcement(
                    target_id=tid,
                    content=message,
                    embed=embed,
                    is_absen=is_absen,
                )
            except Exception as exc:
                logger.error("Gagal broadcast Discord ke %s: %s", tid, exc)

    async def _send_file_to_discord(
        self,
        user_id: int,
        file_bytes: bytes,
        filename: str,
        caption: str = "",
    ) -> None:
        """Mengirim file/dokumen ke channel/DM Discord yang tertaut."""
        if not self.features.get("discord_sync_enabled", True):
            return

        if not self.discord_client or not self.discord_client.is_connected:
            return

        targets: List[str] = []
        if self.db:
            linked = self.db.get_user_discord_targets(user_id)
            targets = [str(item["discord_target_id"]) for item in linked if item.get("discord_target_id")]

        if not targets and getattr(self.discord_client, "default_channel_id", None):
            targets.append(str(self.discord_client.default_channel_id))

        if not targets:
            return

        for tid in targets:
            try:
                await self.discord_client.send_file(
                    target_id=tid,
                    file_bytes=file_bytes,
                    filename=filename,
                    caption=caption,
                )
            except Exception as exc:
                logger.error("Gagal kirim file Discord ke %s: %s", tid, exc)

    async def notify_item(
        self,
        chat_id: int,
        item: Dict[str, Any],
        is_priority: bool = False,
        ethol_client: Optional[Any] = None,
    ) -> bool:
        """Memformat dan mengirim item notifikasi baru (tugas, materi, atau notifikasi ETHOL).

        Format disesuaikan dengan New.txt:
        - Absen dibuka !!!
        - Materi baru ditambahkan !!!
        - Tugas Baru ditambahkan !!!
        """
        client = ethol_client or self.ethol_client
        category = item.get("category", "notifikasi").lower()
        if not self.is_notif_enabled(category):
            logger.debug("[User %s] Notifier kategori '%s' dinonaktifkan di config.json.", chat_id, category)
            return False

        tg_msg = ""
        wa_msg: Optional[str] = None
        send_to_wa = False

        if category == "tugas":
            judul = item.get("judul", "-")
            matkul = item.get("matkul", "-")
            deskripsi = item.get("deskripsi", "").strip()
            deadline = item.get("deadline", "")
            files: List[Dict] = item.get("files", [])
            link = item.get("link", "")

            # Telegram (HTML)
            tg_msg = (
                "📝 <b>Tugas Baru ditambahkan !!!</b>\n\n"
                f"<b>Matkul:</b> {html.escape(matkul)}\n"
                f"<b>Judul:</b> {html.escape(judul)}\n"
                f"<b>Deskripsi:</b>\n{html.escape(deskripsi) if deskripsi else '<i>(Tidak ada deskripsi tambahan)</i>'}"
            )
            if deadline and deadline != "-":
                tg_msg += f"\n\n⏰ <b>Batas Pengumpulan:</b> {html.escape(deadline)}"

            # WhatsApp
            wa_msg = (
                "📝 *Tugas Baru ditambahkan !!!*\n\n"
                f"*Matkul:* {matkul}\n"
                f"*Judul:* {judul}\n"
                f"*Deskripsi:*\n{deskripsi if deskripsi else '(Tidak ada deskripsi tambahan)'}"
            )
            if deadline and deadline != "-":
                wa_msg += f"\n\n⏰ *Batas Pengumpulan:* {deadline}"

            send_to_wa = True

            # Kirim pesan teks deskripsi dulu
            tg_success = await self.send_text(chat_id, tg_msg)
            if send_to_wa and wa_msg:
                await self._send_to_whatsapp(chat_id, wa_msg.strip(), is_absen=False)

            # Discord
            discord_msg = (
                "📝 **Tugas Baru ditambahkan !!!**\n\n"
                f"**Matkul:** {matkul}\n"
                f"**Judul:** {judul}\n"
                f"**Deskripsi:**\n{deskripsi if deskripsi else '(Tidak ada deskripsi tambahan)'}"
            )
            if deadline and deadline != "-":
                discord_msg += f"\n\n⏰ **Batas Pengumpulan:** {deadline}"
            await self._send_to_discord(chat_id, discord_msg.strip(), is_absen=False)

            # Auto-download & kirim file lampiran langsung jika diaktifkan di config
            if self.features.get("auto_download_tugas", True):
                file_sent_count = 0
                if files and client:
                    for f_item in files[:5]:  # maks 5 lampiran
                        f_url = f_item.get("url", "")
                        f_name = f_item.get("nama", "lampiran.pdf")
                        if not f_url:
                            continue
                        try:
                            result = client.download_file(chat_id, f_url)
                            if result:
                                fname, fbytes = result
                                cap_tg = f"📎 <b>Lampiran:</b> {html.escape(fname)}\n📌 <i>{html.escape(judul)}</i>"
                                cap_wa = f"📎 *Lampiran:* {fname}\n📌 _{judul}_"
                                cap_discord = f"📎 **Lampiran:** {fname}\n📌 *{judul}*"
                                await self.send_document(chat_id, fbytes, fname, cap_tg)
                                await self._send_file_to_whatsapp(chat_id, fbytes, fname, cap_wa)
                                await self._send_file_to_discord(chat_id, fbytes, fname, cap_discord)
                                file_sent_count += 1
                        except Exception as exc:
                            logger.error("[User %s] Gagal kirim lampiran tugas %s: %s", chat_id, f_name, exc)

                if not file_sent_count and link and files:
                    fallback_msg = f"🔗 <i>File lampiran dapat diakses di portal ETHOL:</i> {link}"
                    await self.send_text(chat_id, fallback_msg)

            return tg_success

        elif category == "presensi":
            mk_name = item.get("mk_name", "-")
            jam = item.get("jam") or item.get("waktu") or datetime.now().strftime("%H:%M:%S")
            minggu = item.get("minggu", "-")
            minggu_val = str(minggu) if minggu else "-"

            # Telegram (HTML)
            tg_msg = (
                "📢 <b>Presensi dibuka ❗</b>\n\n"
                f"<b>Matkul:</b> {html.escape(mk_name)}\n"
                f"<b>Waktu:</b> {html.escape(str(jam))}\n"
                f"<b>Minggu:</b> {html.escape(minggu_val)}"
            )

            # WhatsApp (plain text, tanpa HTML)
            wa_msg = (
                "📢 Presensi dibuka ❗\n\n"
                f"Matkul: {mk_name}\n"
                f"Waktu: {jam}\n"
                f"Minggu: {minggu_val}"
            )

            # Discord (markdown)
            discord_msg = (
                "📢 Presensi dibuka ❗\n\n"
                f"Matkul: {mk_name}\n"
                f"Waktu: {jam}\n"
                f"Minggu: {minggu_val}"
            )

            tg_success = await self.send_text(chat_id, tg_msg)
            if wa_msg:
                await self._send_to_whatsapp(chat_id, wa_msg.strip(), is_absen=True)
            await self._send_to_discord(chat_id, discord_msg.strip(), is_absen=True)

            return tg_success

        elif category == "materi":
            judul = item.get("judul", "-")
            matkul = item.get("matkul", "-")
            link = item.get("link", "")

            # Telegram (HTML)
            tg_msg = (
                "📚 <b>Materi baru ditambahkan !!!</b>\n\n"
                f"<b>Matkul:</b> {html.escape(matkul)}\n"
                f"<b>Materi:</b> {html.escape(judul)}"
            )

            # WhatsApp
            wa_msg = (
                "📚 *Materi baru ditambahkan !!!*\n\n"
                f"*Matkul:* {matkul}\n"
                f"*Materi:* {judul}"
            )

            # Discord
            discord_msg = (
                "📚 **Materi baru ditambahkan !!!**\n\n"
                f"**Matkul:** {matkul}\n"
                f"**Materi:** {judul}"
            )

            tg_success = await self.send_text(chat_id, tg_msg)
            if wa_msg:
                await self._send_to_whatsapp(chat_id, wa_msg.strip(), is_absen=False)
            await self._send_to_discord(chat_id, discord_msg.strip(), is_absen=False)

            # Auto-download & kirim file materi langsung jika diaktifkan di config
            if self.features.get("auto_download_materi", True):
                file_downloaded = False
                if link and client:
                    try:
                        result = client.download_file(chat_id, link)
                        if result:
                            fname, fbytes = result
                            cap_tg = f"📄 <b>Materi:</b> {html.escape(judul)}\n📖 <i>{html.escape(matkul)}</i>"
                            cap_wa = f"📄 *Materi:* {judul}\n📖 _{matkul}_"
                            cap_discord = f"📄 **Materi:** {judul}\n📖 *{matkul}*"
                            await self.send_document(chat_id, fbytes, fname, cap_tg)
                            await self._send_file_to_whatsapp(chat_id, fbytes, fname, cap_wa)
                            await self._send_file_to_discord(chat_id, fbytes, fname, cap_discord)
                            file_downloaded = True
                    except Exception as exc:
                        logger.error("[User %s] Gagal kirim file materi %s: %s", chat_id, judul, exc)

                if not file_downloaded and link:
                    fallback_msg = f"🔗 <i>Unduh materi melalui portal ETHOL:</i> {link}"
                    await self.send_text(chat_id, fallback_msg)

            return tg_success

        else:
            title = item.get("title", "Notifikasi ETHOL")
            body = item.get("body", "-")
            time_str = item.get("time", "")

            is_task_notif = "tugas" in title.lower() or "tugas" in body.lower()
            notif_label = "Notifikasi Tugas" if is_task_notif else "Notifikasi ETHOL"
            icon = "📌" if is_task_notif else "🔔"

            tg_msg = (
                f"{icon} <b>{html.escape(notif_label)}</b>\n\n"
                f"<b>Judul:</b> {html.escape(title)}\n"
                f"<b>Detail:</b> {html.escape(body)}"
            )
            if time_str:
                tg_msg += f"\n<b>Waktu:</b> {html.escape(time_str)}"

            discord_msg = (
                f"{icon} **{notif_label}**\n\n"
                f"**Judul:** {title}\n"
                f"**Detail:** {body}"
            )
            if time_str:
                discord_msg += f"\n**Waktu:** {time_str}"

            if is_task_notif:
                wa_msg = (
                    f"{icon} *{notif_label}*\n\n"
                    f"*Judul:* {title}\n"
                    f"*Detail:* {body}"
                )
                if time_str:
                    wa_msg += f"\n*Waktu:* {time_str}"
                send_to_wa = True

            tg_success = await self.send_text(chat_id, tg_msg)
            if send_to_wa and wa_msg:
                await self._send_to_whatsapp(chat_id, wa_msg.strip(), is_absen=False)
            await self._send_to_discord(chat_id, discord_msg.strip(), is_absen=False)

            return tg_success

    async def notify_task_submitted(
        self,
        chat_id: int,
        task: Dict[str, Any],
        ethol_client: Optional[Any] = None,
    ) -> bool:
        """Memformat dan mengirim notifikasi saat sebuah tugas sudah dikumpulkan,

        serta otomatis mengunduh file jawaban yang dikumpulkan dan mengirimkannya
        ke Telegram dan WhatsApp langsung.
        """
        if not self.is_notif_enabled("tugas_dikumpulkan"):
            logger.debug("[User %s] Notifier 'tugas_dikumpulkan' dinonaktifkan di config.json.", chat_id)
            return False

        client = ethol_client or self.ethol_client
        matkul = task.get("matkul", "-")
        judul = task.get("judul", "-")
        waktu = task.get("submission_time") or datetime.now().strftime("%d %b %Y, %H:%M WIB")
        catatan = task.get("catatan_jawaban", "").strip()
        submitted_files: List[Dict[str, str]] = task.get("submitted_files", [])

        # Telegram (HTML)
        tg_msg = (
            "✅ <b>Tugas Sudah Dikumpulkan !!!</b>\n\n"
            f"<b>Matkul:</b> {html.escape(matkul)}\n"
            f"<b>Judul:</b> {html.escape(judul)}\n"
            f"<b>Waktu Pengumpulan:</b> {html.escape(str(waktu))}"
        )
        if catatan:
            tg_msg += f"\n<b>Catatan:</b> {html.escape(catatan)}"

        # WhatsApp
        wa_msg = (
            "✅ *Tugas Sudah Dikumpulkan !!!*\n\n"
            f"*Matkul:* {matkul}\n"
            f"*Judul:* {judul}\n"
            f"*Waktu Pengumpulan:* {waktu}"
        )
        if catatan:
            wa_msg += f"\n*Catatan:* {catatan}"

        # 1. Kirim pesan konfirmasi teks terlebih dahulu
        tg_success = await self.send_text(chat_id, tg_msg)
        await self._send_to_whatsapp(chat_id, wa_msg.strip(), is_absen=False)

        discord_msg = (
            "✅ **Tugas Sudah Dikumpulkan !!!**\n\n"
            f"**Matkul:** {matkul}\n"
            f"**Judul:** {judul}\n"
            f"**Waktu Pengumpulan:** {waktu}"
        )
        if catatan:
            discord_msg += f"\n**Catatan:** {catatan}"
        await self._send_to_discord(chat_id, discord_msg.strip(), is_absen=False)

        # 2. Auto-download & kirim file jawaban jika diaktifkan di config
        if self.features.get("auto_download_submitted_task", True) and submitted_files and client:
            for f_item in submitted_files[:5]:
                f_url = f_item.get("url", "")
                f_name = f_item.get("nama", "jawaban_tugas.pdf")
                if not f_url:
                    continue
                try:
                    result = client.download_file(chat_id, f_url)
                    if result:
                        fname, fbytes = result
                        cap_tg = f"📎 <b>File Jawaban:</b> {html.escape(fname)}\n📌 <i>{html.escape(judul)} ({html.escape(matkul)})</i>"
                        cap_wa = f"📎 *File Jawaban:* {fname}\n📌 _{judul} ({matkul})_"
                        cap_discord = f"📎 **File Jawaban:** {fname}\n📌 *{judul} ({matkul})*"
                        await self.send_document(chat_id, fbytes, fname, cap_tg)
                        await self._send_file_to_whatsapp(chat_id, fbytes, fname, cap_wa)
                        await self._send_file_to_discord(chat_id, fbytes, fname, cap_discord)
                except Exception as exc:
                    logger.error("[User %s] Gagal kirim file jawaban tugas %s: %s", chat_id, f_name, exc)

        return tg_success

    async def notify_presensi_result(
        self,
        chat_id: int,
        mk_name: str,
        success: bool,
        message: str,
        jam: Optional[str] = None,
        minggu: Optional[str] = None,
    ) -> bool:
        """Mengirim konfirmasi hasil eksekusi presensi otomatis (sukses / gagal)."""
        if not self.is_notif_enabled("absen"):
            logger.debug("[User %s] Notifier 'absen' dinonaktifkan di config.json.", chat_id)
            return False

        clean_mk = html.escape(mk_name)
        clean_msg = html.escape(message)
        jam_val = str(jam) if jam else datetime.now().strftime("%H:%M:%S")
        minggu_val = str(minggu) if minggu else "-"

        if success:
            tg_text = (
                "📢 <b>Presensi dibuka ❗</b>\n\n"
                f"<b>Matkul:</b> {clean_mk}\n"
                f"<b>Waktu:</b> {html.escape(jam_val)}\n"
                f"<b>Minggu:</b> {html.escape(minggu_val)}"
            )
            wa_text = (
                "📢 Presensi dibuka ❗\n\n"
                f"Matkul: {mk_name}\n"
                f"Waktu: {jam_val}\n"
                f"Minggu: {minggu_val}"
            )
            discord_text = (
                "📢 Presensi dibuka ❗\n\n"
                f"Matkul: {mk_name}\n"
                f"Waktu: {jam_val}\n"
                f"Minggu: {minggu_val}"
            )
        else:
            tg_text = (
                "⚠️ <b>Presensi Gagal Dilakukan</b>\n\n"
                f"<b>Matkul:</b> {clean_mk}\n"
                f"<b>Waktu:</b> {html.escape(jam_val)}\n"
                f"<b>Minggu:</b> {html.escape(minggu_val)}\n\n"
                "<i>Silakan lakukan presensi manual di web ETHOL jika diperlukan.</i>"
            )
            wa_text = (
                "⚠️ Presensi Gagal Dilakukan\n\n"
                f"Matkul: {mk_name}\n"
                f"Waktu: {jam_val}\n"
                f"Minggu: {minggu_val}\n\n"
                "Silakan lakukan presensi manual di web ETHOL jika diperlukan."
            )
            discord_text = (
                "⚠️ **Presensi Gagal Dilakukan**\n\n"
                f"**Matkul:** {mk_name}\n"
                f"**Waktu:** {jam_val}\n"
                f"**Minggu:** {minggu_val}\n\n"
                "*Silakan lakukan presensi manual di web ETHOL jika diperlukan.*"
            )

        tg_success = await self.send_text(chat_id, tg_text)
        await self._send_to_whatsapp(chat_id, wa_text, is_absen=True)
        await self._send_to_discord(chat_id, discord_text, is_absen=True)
        return tg_success

    async def notify_skem(
        self,
        chat_id: int,
        tg_msg: str,
        wa_msg: str,
    ) -> bool:
        """Mengirim notifikasi perubahan status SKEM ke Telegram dan WhatsApp user."""
        if not self.is_notif_enabled("skem"):
            logger.debug("[User %s] Notifier 'skem' dinonaktifkan di config.json.", chat_id)
            return False

        tg_success = await self.send_text(chat_id, tg_msg)
        await self._send_to_whatsapp(chat_id, wa_msg, is_absen=False)
        await self._send_to_discord(chat_id, wa_msg, is_absen=False)
        return tg_success

    # =========================================================================
    # Fitur Simulasi & Uji Coba Bentuk Notifikasi (Test Notif)
    # =========================================================================

    @staticmethod
    def create_sample_pdf(title: str, subtitle: str, content: str) -> bytes:
        """Membuat file contoh PDF valid (PDF-1.4) murni dalam bentuk bytes."""
        content_stream = (
            f"BT\n"
            f"/F1 18 Tf\n"
            f"50 720 Td\n"
            f"({title}) Tj\n"
            f"ET\n"
            f"BT\n"
            f"/F1 12 Tf\n"
            f"50 690 Td\n"
            f"({subtitle}) Tj\n"
            f"ET\n"
            f"BT\n"
            f"/F1 10 Tf\n"
            f"50 650 Td\n"
            f"({content}) Tj\n"
            f"ET\n"
        )
        b_stream = content_stream.encode("latin-1", "replace")
        stream_len = len(b_stream)
        pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
            b"4 0 obj\n<< /Length " + str(stream_len).encode() + b" >>\nstream\n"
            + b_stream
            + b"\nendstream\nendobj\n"
            b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
            b"xref\n0 6\n0000000000 65535 f \n"
            b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n560\n%%EOF\n"
        )
        return pdf

    async def send_test_matkul(
        self,
        chat_id: Optional[int] = None,
        target_platform: str = "all",
        is_group: bool = False,
        wa_chat_id: Optional[str] = None,
        discord_target_id: Optional[str] = None,
    ) -> bool:
        """Mengirim simulasi bentuk notifikasi presensi/matkul dibuka ke target."""
        mk_name = "Workshop Pemrograman Web"
        jam = datetime.now().strftime("%H:%M:%S")
        minggu_val = "14"

        # 1. Telegram
        if target_platform in ("all", "telegram") and chat_id:
            tg_msg = (
                "📢 <b>Presensi dibuka ❗</b>\n\n"
                f"<b>Matkul:</b> {html.escape(mk_name)}\n"
                f"<b>Waktu:</b> {html.escape(str(jam))}\n"
                f"<b>Minggu:</b> {html.escape(minggu_val)}"
            )
            await self.send_text(chat_id, tg_msg)

        # 2. WhatsApp
        if target_platform in ("all", "whatsapp"):
            wa_msg = (
                "📢 Presensi dibuka ❗\n\n"
                f"Matkul: {mk_name}\n"
                f"Waktu: {jam}\n"
                f"Minggu: {minggu_val}"
            )
            if is_group:
                wa_msg += "\n\n@all"

            if wa_chat_id and self.wa_client:
                try:
                    await self.wa_client.send_announcement(wa_chat_id, wa_msg)
                except Exception as exc:
                    logger.error("Gagal kirim test matkul WA ke %s: %s", wa_chat_id, exc)
            elif chat_id:
                await self._send_to_whatsapp(chat_id, wa_msg, is_absen=True)

        # 3. Discord
        if target_platform in ("all", "discord"):
            discord_msg = (
                "📢 Presensi dibuka ❗\n\n"
                f"Matkul: {mk_name}\n"
                f"Waktu: {jam}\n"
                f"Minggu: {minggu_val}"
            )
            if discord_target_id and self.discord_client:
                try:
                    await self.discord_client.send_announcement(discord_target_id, discord_msg, is_absen=True)
                except Exception as exc:
                    logger.error("Gagal kirim test matkul Discord ke %s: %s", discord_target_id, exc)
            elif chat_id:
                await self._send_to_discord(chat_id, discord_msg, is_absen=True)

        return True

    async def send_test_absen(
        self,
        chat_id: Optional[int] = None,
        target_platform: str = "all",
        is_group: bool = False,
        wa_chat_id: Optional[str] = None,
        discord_target_id: Optional[str] = None,
    ) -> bool:
        """Mengirim simulasi bentuk notifikasi hasil presensi/absen tercatat ke target."""
        mk_name = "Workshop Pemrograman Web"
        jam = datetime.now().strftime("%H:%M:%S")
        minggu_val = "14"

        # 1. Telegram
        if target_platform in ("all", "telegram") and chat_id:
            tg_msg = (
                "📢 <b>Presensi dibuka ❗</b>\n\n"
                f"<b>Matkul:</b> {html.escape(mk_name)}\n"
                f"<b>Waktu:</b> {html.escape(str(jam))}\n"
                f"<b>Minggu:</b> {html.escape(minggu_val)}"
            )
            await self.send_text(chat_id, tg_msg)

        # 2. WhatsApp
        if target_platform in ("all", "whatsapp"):
            wa_msg = (
                "📢 Presensi dibuka ❗\n\n"
                f"Matkul: {mk_name}\n"
                f"Waktu: {jam}\n"
                f"Minggu: {minggu_val}"
            )
            if is_group:
                wa_msg += "\n\n@all"

            if wa_chat_id and self.wa_client:
                try:
                    await self.wa_client.send_announcement(wa_chat_id, wa_msg)
                except Exception as exc:
                    logger.error("Gagal kirim test absen WA ke %s: %s", wa_chat_id, exc)
            elif chat_id:
                await self._send_to_whatsapp(chat_id, wa_msg, is_absen=True)

        # 3. Discord
        if target_platform in ("all", "discord"):
            discord_msg = (
                "📢 Presensi dibuka ❗\n\n"
                f"Matkul: {mk_name}\n"
                f"Waktu: {jam}\n"
                f"Minggu: {minggu_val}"
            )
            if discord_target_id and self.discord_client:
                try:
                    await self.discord_client.send_announcement(discord_target_id, discord_msg, is_absen=True)
                except Exception as exc:
                    logger.error("Gagal kirim test absen Discord ke %s: %s", discord_target_id, exc)
            elif chat_id:
                await self._send_to_discord(chat_id, discord_msg, is_absen=True)

        return True

    async def send_test_materi(
        self,
        chat_id: Optional[int] = None,
        target_platform: str = "all",
        is_group: bool = False,
        wa_chat_id: Optional[str] = None,
        discord_target_id: Optional[str] = None,
    ) -> bool:
        """Mengirim simulasi bentuk notifikasi materi baru beserta contoh file PDF."""
        matkul = "Sistem Terdistribusi"
        judul = "Pertemuan 14 - Microservices Architecture"
        filename = "Materi_Pertemuan_14_Microservices.pdf"
        pdf_bytes = self.create_sample_pdf(
            title="Materi Kuliah PENS",
            subtitle=matkul,
            content=f"Contoh Dokumen Bahan Ajar: {judul}",
        )

        # 1. Telegram
        if target_platform in ("all", "telegram") and chat_id:
            tg_msg = (
                "📚 <b>Materi baru ditambahkan !!!</b>\n\n"
                f"<b>Matkul:</b> {html.escape(matkul)}\n"
                f"<b>Materi:</b> {html.escape(judul)}"
            )
            cap_tg = f"📄 <b>Materi:</b> {html.escape(judul)}\n📖 <i>{html.escape(matkul)}</i>"
            await self.send_text(chat_id, tg_msg)
            await self.send_document(chat_id, pdf_bytes, filename, cap_tg)

        # 2. WhatsApp
        if target_platform in ("all", "whatsapp"):
            wa_msg = (
                "📚 *Materi baru ditambahkan !!!*\n\n"
                f"*Matkul:* {matkul}\n"
                f"*Materi:* {judul}"
            )
            cap_wa = f"📄 *Materi:* {judul}\n📖 _{matkul}_"

            if wa_chat_id and self.wa_client:
                try:
                    await self.wa_client.send_announcement(wa_chat_id, wa_msg)
                    await self.wa_client.send_file(wa_chat_id, pdf_bytes, filename, cap_wa)
                except Exception as exc:
                    logger.error("Gagal kirim test materi WA ke %s: %s", wa_chat_id, exc)
            elif chat_id:
                await self._send_to_whatsapp(chat_id, wa_msg, is_absen=False)
                await self._send_file_to_whatsapp(chat_id, pdf_bytes, filename, cap_wa)

        # 3. Discord
        if target_platform in ("all", "discord"):
            discord_msg = (
                "📚 **Materi baru ditambahkan !!!**\n\n"
                f"**Matkul:** {matkul}\n"
                f"**Materi:** {judul}"
            )
            cap_discord = f"📄 **Materi:** {judul}\n📖 *{matkul}*"

            if discord_target_id and self.discord_client:
                try:
                    await self.discord_client.send_announcement(discord_target_id, discord_msg, is_absen=False)
                    await self.discord_client.send_file(discord_target_id, pdf_bytes, filename, cap_discord)
                except Exception as exc:
                    logger.error("Gagal kirim test materi Discord ke %s: %s", discord_target_id, exc)
            elif chat_id:
                await self._send_to_discord(chat_id, discord_msg, is_absen=False)
                await self._send_file_to_discord(chat_id, pdf_bytes, filename, cap_discord)

        return True

    async def send_test_tugas(
        self,
        chat_id: Optional[int] = None,
        target_platform: str = "all",
        is_group: bool = False,
        wa_chat_id: Optional[str] = None,
        discord_target_id: Optional[str] = None,
    ) -> bool:
        """Mengirim simulasi bentuk notifikasi tugas baru beserta contoh file PDF soal."""
        matkul = "Proyek Akhir Terapan"
        judul = "Tugas 4 - Implementasi REST API & Unit Testing"
        deskripsi = "Silakan kumpulkan laporan implementasi kode program serta tautan repositori GitHub sebelum batas waktu."
        deadline = "30 Nov 2026, 23:59 WIB"
        filename = "Petunjuk_Tugas_4_REST_API.pdf"
        pdf_bytes = self.create_sample_pdf(
            title="Petunjuk Tugas Kuliah PENS",
            subtitle=matkul,
            content=f"Contoh Dokumen Soal & Petunjuk: {judul}",
        )

        # 1. Telegram
        if target_platform in ("all", "telegram") and chat_id:
            tg_msg = (
                "📝 <b>Tugas Baru ditambahkan !!!</b>\n\n"
                f"<b>Matkul:</b> {html.escape(matkul)}\n"
                f"<b>Judul:</b> {html.escape(judul)}\n"
                f"<b>Deskripsi:</b>\n{html.escape(deskripsi)}\n\n"
                f"⏰ <b>Batas Pengumpulan:</b> {html.escape(deadline)}"
            )
            cap_tg = f"📎 <b>Lampiran:</b> {html.escape(filename)}\n📌 <i>{html.escape(judul)}</i>"
            await self.send_text(chat_id, tg_msg)
            await self.send_document(chat_id, pdf_bytes, filename, cap_tg)

        # 2. WhatsApp
        if target_platform in ("all", "whatsapp"):
            wa_msg = (
                "📝 *Tugas Baru ditambahkan !!!*\n\n"
                f"*Matkul:* {matkul}\n"
                f"*Judul:* {judul}\n"
                f"*Deskripsi:*\n{deskripsi}\n\n"
                f"⏰ *Batas Pengumpulan:* {deadline}"
            )
            cap_wa = f"📎 *Lampiran:* {filename}\n📌 _{judul}_"

            if wa_chat_id and self.wa_client:
                try:
                    await self.wa_client.send_announcement(wa_chat_id, wa_msg)
                    await self.wa_client.send_file(wa_chat_id, pdf_bytes, filename, cap_wa)
                except Exception as exc:
                    logger.error("Gagal kirim test tugas WA ke %s: %s", wa_chat_id, exc)
            elif chat_id:
                await self._send_to_whatsapp(chat_id, wa_msg, is_absen=False)
                await self._send_file_to_whatsapp(chat_id, pdf_bytes, filename, cap_wa)

        # 3. Discord
        if target_platform in ("all", "discord"):
            discord_msg = (
                "📝 **Tugas Baru ditambahkan !!!**\n\n"
                f"**Matkul:** {matkul}\n"
                f"**Judul:** {judul}\n"
                f"**Deskripsi:**\n{deskripsi}\n\n"
                f"⏰ **Batas Pengumpulan:** {deadline}"
            )
            cap_discord = f"📎 **Lampiran:** {filename}\n📌 *{judul}*"

            if discord_target_id and self.discord_client:
                try:
                    await self.discord_client.send_announcement(discord_target_id, discord_msg, is_absen=False)
                    await self.discord_client.send_file(discord_target_id, pdf_bytes, filename, cap_discord)
                except Exception as exc:
                    logger.error("Gagal kirim test tugas Discord ke %s: %s", discord_target_id, exc)
            elif chat_id:
                await self._send_to_discord(chat_id, discord_msg, is_absen=False)
                await self._send_file_to_discord(chat_id, pdf_bytes, filename, cap_discord)

        return True
