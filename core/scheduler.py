"""Scheduler tugas asinkronus (APScheduler) untuk background polling dan auto-absen multi-user.

Menjalankan siklus polling untuk seluruh pengguna yang aktif login:
- Notifikasi ETHOL (materi, tugas baru, notifikasi perkuliahan)
- Auto-absen kehadiran mahasiswa dengan cooldown per mata kuliah
- Deteksi tugas yang baru dikumpulkan beserta auto-download file jawaban
- SKEM polling setiap 5 menit untuk mahasiswa PJJ dan Reguler
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.database import DatabaseManager
from core.ethol_client import EtholClient
from core.filter_engine import FilterEngine
from core.notifier import Notifier
from utils.crypto import decrypt_string
from utils.logger import setup_logger

logger = setup_logger(__name__)


class BotScheduler:
    """Scheduler background job untuk polling ETHOL dan monitoring SKEM."""

    def __init__(
        self,
        client: EtholClient,
        filter_engine: FilterEngine,
        notifier: Notifier,
        db: DatabaseManager,
        config_path: str = "config.json",
    ) -> None:
        self.client = client
        self.filter_engine = filter_engine
        self.notifier = notifier
        self.db = db
        self.config_path = config_path

        self.poll_interval: int = 5
        self.tugas_poll_interval: int = 60
        self.materi_poll_interval: int = 120
        self.skem_poll_interval: int = 300
        self.cooldown_seconds: int = 300
        self.auto_absen_enabled_global: bool = True
        self.skem_monitor_enabled: bool = True
        self._last_tugas_poll: Dict[int, float] = {}
        self._last_new_tugas_poll: Dict[int, float] = {}
        self._last_materi_poll: Dict[int, float] = {}
        self._last_dashboard_poll: Dict[int, float] = {}
        self._poll_lock = asyncio.Lock()

        self._load_config()

        tz_obj = None
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("Asia/Jakarta")
            tz_obj = "Asia/Jakarta"
        except Exception:
            try:
                import pytz
                tz_obj = pytz.timezone("Asia/Jakarta")
            except Exception:
                tz_obj = None

        if tz_obj:
            self.scheduler = AsyncIOScheduler(timezone=tz_obj)
        else:
            self.scheduler = AsyncIOScheduler()

    def _load_config(self) -> None:
        """Membaca preferensi konfigurasi dan interval polling."""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    polling = cfg.get("polling", {})
                    features = cfg.get("features", {})
                    self.poll_interval = int(polling.get("ethol_poll_interval_seconds", 5))
                    self.tugas_poll_interval = int(polling.get("tugas_poll_interval_seconds", 60))
                    self.materi_poll_interval = int(polling.get("materi_poll_interval_seconds", 120))
                    self.skem_poll_interval = int(polling.get("skem_poll_interval_seconds", 300))
                    self.cooldown_seconds = int(polling.get("presensi_cooldown_seconds", 300))
                    self.auto_absen_enabled_global = bool(features.get("auto_absen_enabled", True))
                    self.skem_monitor_enabled = bool(features.get("skem_monitor_enabled", True))
        except Exception as exc:
            logger.warning("Gagal memuat interval scheduler dari config (%s). Default digunakan.", exc)

    def start(self) -> None:
        """Memulai background scheduler."""
        if not self.scheduler.running:
            self.scheduler.add_job(
                self.poll_job,
                "interval",
                seconds=self.poll_interval,
                id="ethol_polling_job",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=15,
            )

            if self.skem_monitor_enabled:
                self.scheduler.add_job(
                    self.skem_poll_job,
                    "interval",
                    seconds=self.skem_poll_interval,
                    id="skem_polling_job",
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=120,
                )

            self.scheduler.start()
            logger.info(
                "Scheduler multi-user aktif. ETHOL: %ds, SKEM: %ds.",
                self.poll_interval,
                self.skem_poll_interval,
            )

    def stop(self) -> None:
        """Menghentikan background scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler dihentikan.")

    async def poll_job(self) -> None:
        """Siklus utama polling berkala untuk seluruh pengguna aktif."""
        if self._poll_lock.locked():
            logger.debug("Siklus polling sebelumnya masih berlangsung. Melewati trigger ini.")
            return

        async with self._poll_lock:
            active_users = self.db.get_active_logged_in_users()
            if not active_users:
                logger.debug("Tidak ada user aktif untuk polling pada siklus ini.")
                return

            for user in active_users:
                user_id = user["user_id"]
                enc_u = user.get("encrypted_username")
                enc_p = user.get("encrypted_password")

                if not enc_u or not enc_p:
                    continue

                try:
                    username = decrypt_string(enc_u)
                    password = decrypt_string(enc_p)
                except Exception as exc:
                    logger.error("[User %s] Kredensial korup: %s", user_id, exc)
                    continue

                try:
                    # 1. Pastikan session aktif (Auto re-login jika expired)
                    is_active = await asyncio.to_thread(self.client.ensure_session, user_id, username, password)
                    if not is_active:
                        continue

                    # 2. Ambil data notifikasi dashboard (interval 20 detik agar performa ringan & efisien)
                    now_ts = time.time()
                    last_dash = self._last_dashboard_poll.get(user_id, 0.0)
                    new_sent_count = 0
                    current_hash = ""

                    if self.notifier.is_notif_enabled("notifikasi_umum") and (now_ts - last_dash >= 20.0):
                        self._last_dashboard_poll[user_id] = now_ts
                        dashboard_data = await asyncio.to_thread(self.client.fetch_dashboard, user_id)
                        current_hash = dashboard_data.get("content_hash", "")
                        last_hash = user.get("last_seen_hash", "")

                        # 3. Notifikasi dashboard
                        if current_hash and current_hash != last_hash:
                            notifications = dashboard_data.get("notifications", [])
                            for item in notifications:
                                should_send, reason, is_priority = self.filter_engine.should_notify_user(
                                    user_id, item, self.db
                                )
                                if should_send:
                                    sent = await self.notifier.notify_item(
                                        user_id, item, is_priority=is_priority, ethol_client=self.client
                                    )
                                    if sent:
                                        new_sent_count += 1

                    # 4. Auto-Absen & Poller Presensi (Dijalankan setiap 5 detik dengan respons kilat)
                    if self.auto_absen_enabled_global:
                        await self._process_user_auto_absen(user_id)

                    # 5. Deteksi tugas baru (interval configurable, default 60 detik)
                    if self.notifier.is_notif_enabled("tugas"):
                        await self._process_user_new_tasks(user_id)

                    # 6. Deteksi materi baru (interval configurable, default 120 detik)
                    if self.notifier.is_notif_enabled("materi"):
                        await self._process_user_new_materi(user_id)

                    # 7. Deteksi tugas yang baru dikumpulkan (interval 300 detik)
                    if self.notifier.is_notif_enabled("tugas_dikumpulkan"):
                        await self._process_user_submitted_tasks(user_id)

                    # 6. Update metrik polling di DB jika ada update
                    if new_sent_count or current_hash:
                        self.db.update_user_poll_metrics(
                            user_id=user_id,
                            last_seen_hash=current_hash if current_hash else None,
                            last_poll_at=datetime.now().isoformat(),
                            increment_sent=new_sent_count,
                        )

                except Exception as exc:
                    logger.error("[User %s] Error saat memproses polling: %s", user_id, exc, exc_info=True)

            # Pembersihan TTL berkala
            try:
                self.db.clean_expired_notifications()
                self.db.clean_expired_cooldowns()
                self.db.clean_expired_wa_codes()
            except Exception:
                pass

    async def _process_user_auto_absen(self, user_id: int) -> None:
        """Memeriksa sesi presensi aktif dan submit otomatis (berjalan tiap 5 detik)."""
        if not self.db.is_auto_absen_enabled(user_id):
            return

        active_sessions = await asyncio.to_thread(self.client.fetch_presensi_active, user_id)
        courses = self.client.course_cache.get(user_id, [])
        course_count = len(courses) if courses else 0

        if not active_sessions:
            logger.info(
                "[Poller] Refresh absensi berhasil (User %s | %d matkul dipantau - Status: Standby)",
                user_id,
                course_count,
            )
            return

        now = time.time()
        for presensi in active_sessions:
            mk_code = str(presensi.get("mk_code", ""))
            mk_name = presensi.get("mk_name", "Mata Kuliah")
            key = str(presensi.get("key", ""))
            minggu = presensi.get("minggu", "-")

            if key and self.db.is_presensi_session_completed(user_id, key):
                continue

            last_absen = self.db.get_presensi_cooldown(user_id, mk_code)
            elapsed = now - last_absen

            if elapsed < self.cooldown_seconds:
                continue

            logger.info(
                "🚨 [Poller] Terdeteksi presensi terbuka: %s (Minggu %s) untuk User %s! Memproses auto-absen...",
                mk_name,
                minggu,
                user_id,
            )
            result = await asyncio.to_thread(self.client.submit_absen, user_id, presensi)
            success = result.get("success", False)
            already_attended = result.get("already_attended", False)
            msg = result.get("message", "")

            status_log = "SUCCESS" if (success and not already_attended) else ("ALREADY" if already_attended else "FAILED")
            self.db.log_presensi(user_id, mk_code, mk_name, key, status_log, msg)

            jam_str = str(presensi.get("jam") or presensi.get("waktu") or datetime.now().strftime("%H:%M:%S"))
            minggu_val = str(presensi.get("minggu") or "-")

            if success and not already_attended:
                logger.info("✅ [Poller] Auto-absen berhasil dicatat untuk %s (User %s)", mk_name, user_id)
                await self.notifier.notify_presensi_result(
                    chat_id=user_id,
                    mk_name=mk_name,
                    success=True,
                    message=msg,
                    jam=jam_str,
                    minggu=minggu_val,
                )
                self.db.set_presensi_cooldown(user_id, mk_code, now + 43200)
            elif already_attended:
                logger.info("ℹ️ [Poller] Sudah pernah absen untuk %s (User %s)", mk_name, user_id)
                self.db.set_presensi_cooldown(user_id, mk_code, now + 43200)
            else:
                logger.warning("⚠️ [Poller] Auto-absen gagal untuk %s (User %s): %s", mk_name, user_id, msg)
                await self.notifier.notify_presensi_result(
                    chat_id=user_id,
                    mk_name=mk_name,
                    success=False,
                    message=msg,
                    jam=jam_str,
                    minggu=minggu_val,
                )
                self.db.set_presensi_cooldown(user_id, mk_code, now)

    async def _process_user_new_tasks(self, user_id: int) -> None:
        """Memeriksa tugas baru yang belum diketahui dan mengirimkan notifikasi lengkap + lampiran."""
        now = time.time()
        last_check = self._last_new_tugas_poll.get(user_id, 0.0)
        if now - last_check < float(self.tugas_poll_interval):
            return
        self._last_new_tugas_poll[user_id] = now

        try:
            tasks = await asyncio.to_thread(self.client.fetch_tugas, user_id, True, None, True)
            if not tasks:
                return

            is_seeded = self.db.has_known_items_seeded(user_id, "tugas")
            if not is_seeded:
                seed_ids: List[str] = []
                active_to_notify: List[Dict[str, Any]] = []
                for t in tasks:
                    tid = str(t.get("raw_id") or t.get("id"))
                    is_sub = bool(t.get("is_submitted"))
                    is_over = bool(t.get("is_overdue"))
                    hours = int(t.get("hours_left", 9999))
                    # Tugas aktif yang belum dikumpulkan & punya deadline mendatang langsung dinotifikasi
                    if not is_sub and not is_over and hours < 9999:
                        active_to_notify.append(t)
                    else:
                        seed_ids.append(tid)

                if seed_ids:
                    self.db.mark_known_items_bulk(user_id, "tugas", seed_ids)

                # Jika seluruh tugas adalah tugas lama, pastikan minimal 1 marker agar status seeded aktif
                if not seed_ids and not active_to_notify:
                    return

                for t in active_to_notify:
                    tid = str(t.get("raw_id") or t.get("id"))
                    should_send, _, is_priority = self.filter_engine.should_notify_user(user_id, t, self.db)
                    if should_send:
                        logger.info("📝 [Poller] Tugas aktif terdeteksi: %s (%s) untuk User %s", t.get("judul"), t.get("matkul"), user_id)
                        await self.notifier.notify_item(user_id, t, is_priority=is_priority, ethol_client=self.client)
                    self.db.mark_known_item(user_id, "tugas", tid)
                return

            for t in tasks:
                tid = str(t.get("raw_id") or t.get("id"))
                if self.db.is_known_item(user_id, "tugas", tid):
                    continue

                # Tugas baru terdeteksi!
                self.db.mark_known_item(user_id, "tugas", tid)
                if t.get("is_submitted"):
                    continue

                should_send, reason, is_priority = self.filter_engine.should_notify_user(user_id, t, self.db)
                if should_send:
                    logger.info("📝 [Poller] Tugas baru terdeteksi: %s (%s) untuk User %s", t.get("judul"), t.get("matkul"), user_id)
                    await self.notifier.notify_item(user_id, t, is_priority=is_priority, ethol_client=self.client)
                else:
                    logger.debug("[User %s] Tugas baru %s dilewati filter: %s", user_id, t.get("judul"), reason)

        except Exception as exc:
            logger.error("[User %s] Error deteksi tugas baru: %s", user_id, exc)

    async def _process_user_new_materi(self, user_id: int) -> None:
        """Memeriksa materi kuliah baru dan mengirimkan notifikasi + file materi."""
        now = time.time()
        last_check = self._last_materi_poll.get(user_id, 0.0)
        if now - last_check < float(self.materi_poll_interval):
            return
        self._last_materi_poll[user_id] = now

        try:
            materials = await asyncio.to_thread(self.client.fetch_materi, user_id)
            if not materials:
                return

            is_seeded = self.db.has_known_items_seeded(user_id, "materi")
            if not is_seeded:
                seed_ids = [str(m.get("raw_id") or m.get("id")) for m in materials]
                self.db.mark_known_items_bulk(user_id, "materi", seed_ids)
                logger.info("[User %s] Inisialisasi baseline %d materi tersimpan.", user_id, len(seed_ids))
                return

            for m in materials:
                mid = str(m.get("raw_id") or m.get("id"))
                if self.db.is_known_item(user_id, "materi", mid):
                    continue

                self.db.mark_known_item(user_id, "materi", mid)
                should_send, reason, is_priority = self.filter_engine.should_notify_user(user_id, m, self.db)
                if should_send:
                    logger.info("📚 [Poller] Materi baru terdeteksi: %s (%s) untuk User %s", m.get("judul"), m.get("matkul"), user_id)
                    await self.notifier.notify_item(user_id, m, is_priority=is_priority, ethol_client=self.client)
                else:
                    logger.debug("[User %s] Materi baru %s dilewati filter: %s", user_id, m.get("judul"), reason)

        except Exception as exc:
            logger.error("[User %s] Error deteksi materi baru: %s", user_id, exc)

    async def _process_user_submitted_tasks(self, user_id: int) -> None:
        """Memeriksa tugas yang baru dikumpulkan oleh mahasiswa (dibatasi interval 5 menit)."""
        now = time.time()
        last_check = self._last_tugas_poll.get(user_id, 0.0)
        if now - last_check < 300.0:
            return
        self._last_tugas_poll[user_id] = now

        try:
            tasks = await asyncio.to_thread(self.client.fetch_tugas, user_id, True)
            if not tasks:
                return

            submitted_tasks = [t for t in tasks if t.get("is_submitted")]
            is_seeded = self.db.is_user_tasks_seeded(user_id)

            if not is_seeded:
                for t in submitted_tasks:
                    raw_id = str(t.get("raw_id") or t.get("id"))
                    sub_time = str(t.get("submission_time") or "")
                    sub_file = str(t.get("submission_file") or "")
                    self.db.record_submitted_task(user_id, raw_id, sub_time, sub_file)
                self.db.set_user_tasks_seeded(user_id, True)
                logger.info(
                    "[User %s] Inisialisasi baseline %d tugas tersimpan.",
                    user_id,
                    len(submitted_tasks),
                )
                return

            for t in submitted_tasks:
                raw_id = str(t.get("raw_id") or t.get("id"))
                sub_time = str(t.get("submission_time") or "")
                sub_file = str(t.get("submission_file") or "")

                if not self.db.is_task_submission_tracked(user_id, raw_id):
                    logger.info("[User %s] Tugas dikumpulkan terdeteksi: %s (%s)", user_id, t.get("judul"), t.get("matkul"))
                    await self.notifier.notify_task_submitted(user_id, t, ethol_client=self.client)
                    self.db.record_submitted_task(user_id, raw_id, sub_time, sub_file)

        except Exception as exc:
            logger.error("[User %s] Error deteksi tugas dikumpulkan: %s", user_id, exc)

    async def skem_poll_job(self) -> None:
        """Background job untuk polling monitoring SKEM."""
        if not self.notifier.is_notif_enabled("skem"):
            return
        try:
            from core.skem_monitor import (
                detect_skem_changes,
                fetch_skem_data,
                format_new_skem_notification,
                format_skem_notification,
            )
        except ImportError:
            return

        monitors = self.db.get_all_skem_monitors()
        if not monitors:
            return

        for monitor in monitors:
            monitor_id = monitor["id"]
            user_id = monitor["user_id"]
            nrp = monitor["nrp"]
            portal = monitor.get("portal", "auto")
            last_seen_json = monitor.get("last_seen_json", "[]")

            try:
                new_data = await asyncio.to_thread(fetch_skem_data, nrp, portal)
                if not new_data:
                    continue

                nama = new_data.get("nama", "-")

                if last_seen_json in ("[]", "", None):
                    new_aktivitas_json = json.dumps(new_data.get("aktivitas", []), ensure_ascii=False)
                    self.db.update_skem_last_seen(monitor_id, new_aktivitas_json)
                    continue

                changes = detect_skem_changes(nrp, last_seen_json, new_data)
                if changes:
                    total_kredit = new_data.get("nilai_kredit", "0")
                    for change in changes:
                        validasi = change.get("validasi", "").lower()
                        change_type = change.get("change_type", "")

                        if change_type == "new":
                            tg_msg, wa_msg = format_new_skem_notification(nama, nrp, change)
                        elif validasi in ("disetujui", "diterima", "ditolak"):
                            tg_msg, wa_msg = format_skem_notification(nama, nrp, change, total_kredit=total_kredit)
                        else:
                            continue

                        await self.notifier.notify_skem(user_id, tg_msg, wa_msg)

                new_aktivitas_json = json.dumps(new_data.get("aktivitas", []), ensure_ascii=False)
                self.db.update_skem_last_seen(monitor_id, new_aktivitas_json)

            except Exception as exc:
                logger.error("[SKEM] Error polling NRP %s: %s", nrp, exc)
