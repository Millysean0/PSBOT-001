"""Database SQLite terpusat untuk arsitektur multi-user PSBOT-001.

Mengelola tabel pengguna (users), kredensial terenkripsi per pengguna,
pengaturan filter dinamis, cooldown presensi, deduplikasi notifikasi,
tautan WhatsApp, pelacakan pengawasan SKEM, dan tugas yang dikumpulkan.
Menggunakan mode WAL (Write-Ahead Logging) untuk performa tinggi dan efisiensi memori.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Set

from utils.logger import setup_logger

logger = setup_logger(__name__)


class DatabaseManager:
    """Pengelola database SQLite untuk sistem multi-user."""

    def __init__(self, db_path: str = "ethol_bot.db") -> None:
        """Inisialisasi DatabaseManager.

        Args:
            db_path: Path lokasi file SQLite database atau ':memory:'.
        """
        self.db_path = str(db_path)
        self._lock = threading.Lock()

        # Koneksi persisten dengan autocommit & thread safety
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=20.0,
            check_same_thread=False,
            isolation_level=None,  # Autocommit mode
        )
        self._conn.row_factory = sqlite3.Row

        if self.db_path != ":memory:":
            try:
                self._conn.execute("PRAGMA journal_mode = WAL;")
            except Exception:
                pass
        self._conn.execute("PRAGMA synchronous = NORMAL;")
        self._conn.execute("PRAGMA foreign_keys = ON;")

        self.init_db()

    @contextlib.contextmanager
    def get_connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager thread-safe untuk mengakses koneksi SQLite."""
        with self._lock:
            yield self._conn

    def close(self) -> None:
        """Menutup koneksi database."""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def init_db(self) -> None:
        """Membuat tabel dan indeks database jika belum ada."""
        with self.get_connection() as conn:
            # 1. Tabel Users
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    telegram_username TEXT,
                    first_name TEXT,
                    encrypted_username TEXT DEFAULT '',
                    encrypted_password TEXT DEFAULT '',
                    ethol_username_display TEXT DEFAULT '',
                    nrp TEXT DEFAULT '',
                    nama TEXT DEFAULT '',
                    is_logged_in INTEGER DEFAULT 0,
                    is_paused INTEGER DEFAULT 0,
                    auto_absen_enabled INTEGER DEFAULT 1,
                    tasks_seeded INTEGER DEFAULT 0,
                    last_poll_at TEXT,
                    last_login_at TEXT,
                    last_seen_hash TEXT DEFAULT '',
                    sent_count_total INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                );
                """
            )

            # Migrasi tabel users: pastikan kolom nrp dan nama ada
            for col in ["nrp", "nama"]:
                try:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT '';")
                except Exception:
                    pass

            # 2. Tabel User Filters (Blacklist, Whitelist, Watchlist, Muted Categories)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_filters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    type TEXT NOT NULL,  -- 'exclude', 'include', 'watchlist', 'muted'
                    value TEXT NOT NULL,
                    created_at TEXT,
                    UNIQUE(user_id, type, value),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )

            # 3. Tabel Cooldown Presensi per User per Mata Kuliah
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS presensi_cooldown (
                    user_id INTEGER NOT NULL,
                    mk_code TEXT NOT NULL,
                    last_absen_time REAL NOT NULL,
                    PRIMARY KEY (user_id, mk_code),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )

            # 4. Tabel Deduplikasi Notifikasi per User (TTL 24 jam)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_notifications (
                    user_id INTEGER NOT NULL,
                    item_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (user_id, item_hash),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )

            # 5. Tabel Riwayat Presensi per User (Auto-Absen & Manual)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS presensi_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    mk_code TEXT NOT NULL,
                    mk_name TEXT NOT NULL,
                    session_key TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    message TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )

            # 6. Tabel Kode Verifikasi / Tautkan WhatsApp (-connect)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wa_connect_codes (
                    code TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )

            # 7. Tabel Tautan WhatsApp Pengguna / Chat / Grup
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_whatsapp_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    wa_chat_id TEXT NOT NULL,
                    chat_type TEXT NOT NULL DEFAULT 'private',
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, wa_chat_id),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )

            # 8. Tabel SKEM Monitor per User
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS skem_monitors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    nrp TEXT NOT NULL,
                    portal TEXT DEFAULT 'auto',
                    last_seen_json TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, nrp),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )

            # 9. Tabel Submitted Task Tracking (Untuk notifikasi tugas yang baru dikumpulkan)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS submitted_task_tracking (
                    user_id INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    submission_time TEXT DEFAULT '',
                    submission_file TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    PRIMARY KEY (user_id, task_id),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )

            # 10. Tabel Tautan Discord Pengguna / Channel
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_discord_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    discord_target_id TEXT NOT NULL,
                    target_type TEXT NOT NULL DEFAULT 'channel',
                    guild_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, discord_target_id)
                );
                """
            )

            # 11. Tabel Known Academic Items (Tugas baru / Materi baru agar tidak kedaluwarsa & tidak spam)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS known_academic_items (
                    user_id INTEGER NOT NULL,
                    item_type TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (user_id, item_type, item_id)
                );
                """
            )

            # Indeks performa
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users (is_logged_in, is_paused);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_notif_time ON seen_notifications (created_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_presensi_logs_user ON presensi_logs (user_id, created_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_links_user ON user_whatsapp_links (user_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_links_chat ON user_whatsapp_links (wa_chat_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_codes_exp ON wa_connect_codes (expires_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_discord_links_user ON user_discord_links (user_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_discord_links_target ON user_discord_links (discord_target_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_skem_user ON skem_monitors (user_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_submitted_task_user ON submitted_task_tracking (user_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_known_items_user ON known_academic_items (user_id, item_type);")

        logger.info("Database SQLite multi-user (%s) berhasil diinisialisasi.", self.db_path)

    # =========================================================================
    # Operasi User (Pengguna)
    # =========================================================================

    def get_or_create_user(
        self,
        user_id: int,
        telegram_username: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Mendapatkan data user, atau membuat entri baru jika belum ada di database."""
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,))
            row = cursor.fetchone()

            if row:
                conn.execute(
                    """
                    UPDATE users
                    SET telegram_username = COALESCE(?, telegram_username),
                        first_name = COALESCE(?, first_name),
                        updated_at = ?
                    WHERE user_id = ?;
                    """,
                    (telegram_username, first_name, now_str, user_id),
                )
                cursor = conn.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,))
                return dict(cursor.fetchone())

            conn.execute(
                """
                INSERT INTO users (
                    user_id, telegram_username, first_name, is_logged_in, is_paused,
                    auto_absen_enabled, tasks_seeded, created_at, updated_at
                ) VALUES (?, ?, ?, 0, 0, 1, 0, ?, ?);
                """,
                (user_id, telegram_username or "", first_name or "", now_str, now_str),
            )
            # Filter default agar pengumuman uji coba/ditiadakan terfilter
            for kw in ["batal", "uji coba", "test", "ditiadakan"]:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO user_filters (user_id, type, value, created_at)
                    VALUES (?, 'exclude', ?, ?);
                    """,
                    (user_id, kw, now_str),
                )

            cursor = conn.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,))
            return dict(cursor.fetchone())

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Mengambil data pengguna berdasarkan Telegram user_id."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_active_logged_in_users(self) -> List[Dict[str, Any]]:
        """Mengambil semua pengguna yang sedang login dan tidak dalam status jeda (is_paused = 0)."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM users
                WHERE is_logged_in = 1 AND is_paused = 0 AND encrypted_username != '';
                """
            )
            return [dict(r) for r in cursor.fetchall()]

    def update_user_credentials(
        self,
        user_id: int,
        enc_username: str,
        enc_password: str,
        ethol_display: str,
        nrp: str = "",
        nama: str = "",
    ) -> None:
        """Menyimpan kredensial terenkripsi dan menandai user telah login."""
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET encrypted_username = ?,
                    encrypted_password = ?,
                    ethol_username_display = ?,
                    nrp = CASE WHEN ? != '' THEN ? ELSE nrp END,
                    nama = CASE WHEN ? != '' THEN ? ELSE nama END,
                    is_logged_in = 1,
                    last_login_at = ?,
                    updated_at = ?
                WHERE user_id = ?;
                """,
                (enc_username, enc_password, ethol_display, nrp, nrp, nama, nama, now_str, now_str, user_id),
            )

    def get_user_nrp(self, user_id: int) -> str:
        """Mengambil nomor NRP mahasiswa dari tabel users atau skem_monitors."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT nrp FROM users WHERE user_id = ?;", (user_id,))
            row = cursor.fetchone()
            if row and row["nrp"]:
                return str(row["nrp"]).strip()
            # Cek di skem_monitors jika ada
            cursor = conn.execute("SELECT nrp FROM skem_monitors WHERE user_id = ? ORDER BY id DESC LIMIT 1;", (user_id,))
            row_s = cursor.fetchone()
            if row_s and row_s["nrp"]:
                return str(row_s["nrp"]).strip()
            # Cek ethol_username_display jika berupa angka
            cursor = conn.execute("SELECT ethol_username_display FROM users WHERE user_id = ?;", (user_id,))
            row_u = cursor.fetchone()
            if row_u and row_u["ethol_username_display"]:
                disp = str(row_u["ethol_username_display"]).strip()
                if disp.isdigit() and len(disp) >= 6:
                    return disp
        return ""

    def logout_user(self, user_id: int) -> None:
        """Menghapus kredensial terenkripsi pengguna (Logout)."""
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET encrypted_username = '',
                    encrypted_password = '',
                    is_logged_in = 0,
                    tasks_seeded = 0,
                    updated_at = ?
                WHERE user_id = ?;
                """,
                (now_str, user_id),
            )
            conn.execute("DELETE FROM submitted_task_tracking WHERE user_id = ?;", (user_id,))

    def set_user_paused(self, user_id: int, is_paused: bool) -> None:
        """Mengatur status pause / resume polling untuk pengguna tertentu."""
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET is_paused = ?, updated_at = ?
                WHERE user_id = ?;
                """,
                (1 if is_paused else 0, now_str, user_id),
            )

    def is_auto_absen_enabled(self, user_id: int) -> bool:
        """Memeriksa apakah fitur auto-absen aktif untuk pengguna."""
        user = self.get_user(user_id)
        if not user:
            return True
        return bool(user.get("auto_absen_enabled", 1))

    def set_auto_absen_enabled(self, user_id: int, enabled: bool) -> None:
        """Mengatur status aktif/nonaktif auto-absen pengguna."""
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET auto_absen_enabled = ?, updated_at = ?
                WHERE user_id = ?;
                """,
                (1 if enabled else 0, now_str, user_id),
            )

    def toggle_auto_absen(self, user_id: int) -> bool:
        """Toggle status aktif/nonaktif auto-absen pengguna dan mengembalikan status barunya."""
        current = self.is_auto_absen_enabled(user_id)
        new_val = not current
        self.set_auto_absen_enabled(user_id, new_val)
        return new_val

    def update_user_poll_metrics(
        self,
        user_id: int,
        last_seen_hash: Optional[str] = None,
        last_poll_at: Optional[str] = None,
        increment_sent: int = 0,
    ) -> None:
        """Memperbarui metrik polling pengguna."""
        now_str = last_poll_at or datetime.now().isoformat()
        with self.get_connection() as conn:
            if last_seen_hash:
                conn.execute(
                    """
                    UPDATE users
                    SET last_seen_hash = ?,
                        last_poll_at = ?,
                        sent_count_total = sent_count_total + ?,
                        updated_at = ?
                    WHERE user_id = ?;
                    """,
                    (last_seen_hash, now_str, increment_sent, now_str, user_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET last_poll_at = ?,
                        sent_count_total = sent_count_total + ?,
                        updated_at = ?
                    WHERE user_id = ?;
                    """,
                    (now_str, increment_sent, now_str, user_id),
                )

    # =========================================================================
    # Operasi Filter & Preferensi Pengguna
    # =========================================================================

    def get_user_filters(self, user_id: int) -> Dict[str, Set[str]]:
        """Mengambil seluruh aturan filter pengguna (exclude, watchlist, muted)."""
        result: Dict[str, Set[str]] = {
            "exclude": set(),
            "watchlist": set(),
            "muted": set(),
        }
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT type, value FROM user_filters WHERE user_id = ?;",
                (user_id,),
            )
            for row in cursor.fetchall():
                f_type = row["type"]
                if f_type in result:
                    result[f_type].add(row["value"].lower().strip())
        return result

    def add_user_filter(self, user_id: int, filter_type: str, value: str) -> bool:
        """Menambahkan filter untuk pengguna."""
        clean_val = value.lower().strip()
        if not clean_val:
            return False
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO user_filters (user_id, type, value, created_at)
                    VALUES (?, ?, ?, ?);
                    """,
                    (user_id, filter_type, clean_val, now_str),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def remove_user_filter(self, user_id: int, filter_type: str, value: str) -> bool:
        """Menghapus filter untuk pengguna."""
        clean_val = value.lower().strip()
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM user_filters
                WHERE user_id = ? AND type = ? AND value = ?;
                """,
                (user_id, filter_type, clean_val),
            )
            return cursor.rowcount > 0

    def toggle_user_mute(self, user_id: int, category: str) -> bool:
        """Toggle mute/unmute untuk suatu kategori. Mengembalikan True jika sekarang di-mute."""
        clean_cat = category.lower().strip()
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id FROM user_filters
                WHERE user_id = ? AND type = 'muted' AND value = ?;
                """,
                (user_id, clean_cat),
            )
            row = cursor.fetchone()
            if row:
                conn.execute("DELETE FROM user_filters WHERE id = ?;", (row["id"],))
                return False
            else:
                now_str = datetime.now().isoformat()
                conn.execute(
                    """
                    INSERT INTO user_filters (user_id, type, value, created_at)
                    VALUES (?, 'muted', ?, ?);
                    """,
                    (user_id, clean_cat, now_str),
                )
                return True

    # =========================================================================
    # Operasi Cooldown Presensi
    # =========================================================================

    def get_presensi_cooldown(self, user_id: int, mk_code: str) -> float:
        """Mendapatkan timestamp presensi terakhir pengguna untuk mata kuliah tertentu."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT last_absen_time FROM presensi_cooldown
                WHERE user_id = ? AND mk_code = ?;
                """,
                (user_id, mk_code),
            )
            row = cursor.fetchone()
            return float(row["last_absen_time"]) if row else 0.0

    def set_presensi_cooldown(self, user_id: int, mk_code: str, timestamp: float) -> None:
        """Menyimpan timestamp presensi terakhir pengguna untuk mata kuliah tertentu."""
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO presensi_cooldown (user_id, mk_code, last_absen_time)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, mk_code) DO UPDATE SET last_absen_time = excluded.last_absen_time;
                """,
                (user_id, mk_code, timestamp),
            )

    def clean_expired_cooldowns(self, max_age_seconds: float = 86400.0) -> None:
        """Menghapus entri cooldown yang lebih lama dari 24 jam."""
        cutoff = time.time() - max_age_seconds
        with self.get_connection() as conn:
            conn.execute("DELETE FROM presensi_cooldown WHERE last_absen_time < ?;", (cutoff,))

    # =========================================================================
    # Operasi Log Presensi (Auto-Absen & Manual)
    # =========================================================================

    def log_presensi(
        self,
        user_id: int,
        mk_code: str,
        mk_name: str,
        session_key: str,
        status: str,
        message: str = "",
    ) -> None:
        """Mencatat aktivitas presensi pengguna ke dalam tabel presensi_logs."""
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO presensi_logs (user_id, mk_code, mk_name, session_key, status, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (user_id, str(mk_code), str(mk_name), str(session_key or ""), str(status), str(message), now_str),
            )

    def is_presensi_session_completed(self, user_id: int, session_key: str) -> bool:
        """Memeriksa apakah sesi presensi tertentu dengan key tersebut sudah sukses diabsen."""
        if not session_key:
            return False
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT 1 FROM presensi_logs
                WHERE user_id = ? AND session_key = ? AND status IN ('SUCCESS', 'ALREADY', 'MANUAL_SUCCESS')
                LIMIT 1;
                """,
                (user_id, str(session_key)),
            )
            return cursor.fetchone() is not None

    def get_recent_presensi_logs(self, user_id: int, limit: int = 5) -> List[Dict[str, Any]]:
        """Mengambil riwayat log presensi terbaru untuk pengguna tertentu."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, mk_code, mk_name, session_key, status, message, created_at
                FROM presensi_logs
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?;
                """,
                (user_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Operasi Deduplikasi Notifikasi
    # =========================================================================

    def is_notification_seen(self, user_id: int, item_hash: str, ttl_seconds: float = 86400.0) -> bool:
        """Memeriksa apakah notifikasi sudah pernah dikirim ke user dalam rentang TTL (24 jam)."""
        cutoff = time.time() - ttl_seconds
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT created_at FROM seen_notifications
                WHERE user_id = ? AND item_hash = ? AND created_at >= ?;
                """,
                (user_id, item_hash, cutoff),
            )
            return cursor.fetchone() is not None

    def mark_notification_seen(self, user_id: int, item_hash: str) -> None:
        """Menandai notifikasi telah terkirim ke pengguna dengan timestamp saat ini."""
        now = time.time()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO seen_notifications (user_id, item_hash, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, item_hash) DO UPDATE SET created_at = excluded.created_at;
                """,
                (user_id, item_hash, now),
            )

    def clean_expired_notifications(self, ttl_seconds: float = 86400.0) -> None:
        """Membersihkan riwayat notifikasi yang lebih dari 24 jam untuk menghemat disk & RAM."""
        cutoff = time.time() - ttl_seconds
        with self.get_connection() as conn:
            conn.execute("DELETE FROM seen_notifications WHERE created_at < ?;", (cutoff,))

    # =========================================================================
    # Statistik Agregat Multi-User
    # =========================================================================

    def get_stats(self) -> Dict[str, int]:
        """Mengambil statistik global jumlah pengguna."""
        with self.get_connection() as conn:
            total_users = conn.execute("SELECT COUNT(*) FROM users;").fetchone()[0]
            active_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_logged_in = 1;").fetchone()[0]
            paused_users = conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_logged_in = 1 AND is_paused = 1;"
            ).fetchone()[0]
            return {
                "total_users": total_users,
                "active_users": active_users,
                "paused_users": paused_users,
            }

    # =========================================================================
    # Integrasi WhatsApp Tautkan Perangkat (-connect)
    # =========================================================================

    def create_wa_connect_code(self, user_id: int, ttl_seconds: int = 600) -> str:
        """Membuat kode unik sementara (TTL default 10 menit) untuk tautkan WhatsApp.

        Returns:
            str: Kode unik alfanumerik (misal: 'ETH-9A2F1B').
        """
        now = time.time()
        expires_at = now + ttl_seconds
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        unique_suffix = "".join(secrets.choice(chars) for _ in range(6))
        code = f"ETH-{unique_suffix}"

        with self.get_connection() as conn:
            conn.execute("DELETE FROM wa_connect_codes WHERE user_id = ?;", (user_id,))
            conn.execute(
                """
                INSERT INTO wa_connect_codes (code, user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?);
                """,
                (code, user_id, now, expires_at),
            )
        return code

    def validate_and_consume_wa_connect_code(self, raw_code: str) -> Optional[int]:
        """Memvalidasi dan mengonsumsi kode tautkan WhatsApp jika valid dan belum kadaluarsa."""
        code = raw_code.strip().upper()
        if not code.startswith("ETH-"):
            code = f"ETH-{code}"

        now = time.time()
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT user_id, expires_at FROM wa_connect_codes WHERE code = ?;",
                (code,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            user_id = int(row["user_id"])
            expires_at = float(row["expires_at"])
            conn.execute("DELETE FROM wa_connect_codes WHERE code = ?;", (code,))

            if now <= expires_at:
                return user_id
            return None

    def link_user_whatsapp(self, user_id: int, wa_chat_id: str, chat_type: str = "private") -> None:
        """Menautkan chat ID WhatsApp (nomor pribadi atau grup) ke user_id tertentu."""
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO user_whatsapp_links (user_id, wa_chat_id, chat_type, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, wa_chat_id) DO UPDATE SET
                    chat_type = excluded.chat_type,
                    created_at = excluded.created_at;
                """,
                (user_id, wa_chat_id.strip(), chat_type, now_str),
            )

    def get_user_whatsapp_chats(self, user_id: int) -> List[str]:
        """Mengambil semua wa_chat_id yang tertaut ke akun user_id."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT wa_chat_id FROM user_whatsapp_links WHERE user_id = ?;",
                (user_id,),
            )
            return [str(row["wa_chat_id"]) for row in cursor.fetchall()]

    def get_user_whatsapp_links_detail(self, user_id: int) -> List[Dict[str, Any]]:
        """Mengambil informasi detail tautan WhatsApp untuk user tertentu."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM user_whatsapp_links WHERE user_id = ? ORDER BY id DESC;",
                (user_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def unlink_user_whatsapp(self, user_id: int, wa_chat_id: Optional[str] = None) -> int:
        """Memutus tautan WhatsApp milik user (semua atau spesifik wa_chat_id)."""
        with self.get_connection() as conn:
            if wa_chat_id:
                cursor = conn.execute(
                    "DELETE FROM user_whatsapp_links WHERE user_id = ? AND wa_chat_id = ?;",
                    (user_id, wa_chat_id),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM user_whatsapp_links WHERE user_id = ?;",
                    (user_id,),
                )
            return cursor.rowcount

    def unlink_by_wa_chat_id(self, wa_chat_id: str) -> int:
        """Memutus tautan berdasarkan wa_chat_id (dipanggil saat -disconnect di WhatsApp)."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM user_whatsapp_links WHERE wa_chat_id = ?;",
                (wa_chat_id.strip(),),
            )
            return cursor.rowcount

    def clean_expired_wa_codes(self) -> None:
        """Membersihkan kode tautan WhatsApp yang telah kadaluarsa."""
        now = time.time()
        with self.get_connection() as conn:
            conn.execute("DELETE FROM wa_connect_codes WHERE expires_at < ?;", (now,))

    # =========================================================================
    # Integrasi Discord (Tautan Channel & User)
    # =========================================================================

    def link_user_discord(
        self,
        user_id: int,
        discord_target_id: str,
        target_type: str = "channel",
        guild_id: str = "",
    ) -> None:
        """Menautkan target ID Discord (channel_id atau user_id) ke user_id tertentu."""
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO user_discord_links (user_id, discord_target_id, target_type, guild_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, discord_target_id) DO UPDATE SET
                    target_type = excluded.target_type,
                    guild_id = excluded.guild_id,
                    created_at = excluded.created_at;
                """,
                (user_id, str(discord_target_id).strip(), target_type, str(guild_id).strip(), now_str),
            )

    def get_user_discord_targets(self, user_id: int) -> List[Dict[str, Any]]:
        """Mengambil semua discord_target_id yang tertaut ke akun user_id."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM user_discord_links WHERE user_id = ? ORDER BY id DESC;",
                (user_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_user_by_discord_target(self, discord_target_id: str) -> Optional[int]:
        """Mengambil user_id ETHOL berdasarkan discord_target_id (channel atau DM)."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT user_id FROM user_discord_links WHERE discord_target_id = ? LIMIT 1;",
                (str(discord_target_id).strip(),),
            )
            row = cursor.fetchone()
            return int(row["user_id"]) if row else None

    def unlink_user_discord(self, user_id: int, discord_target_id: Optional[str] = None) -> int:
        """Memutus tautan Discord milik user."""
        with self.get_connection() as conn:
            if discord_target_id:
                cursor = conn.execute(
                    "DELETE FROM user_discord_links WHERE user_id = ? AND discord_target_id = ?;",
                    (user_id, str(discord_target_id).strip()),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM user_discord_links WHERE user_id = ?;",
                    (user_id,),
                )
            return cursor.rowcount

    def unlink_by_discord_target_id(self, discord_target_id: str) -> int:
        """Memutus tautan berdasarkan discord_target_id."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM user_discord_links WHERE discord_target_id = ?;",
                (str(discord_target_id).strip(),),
            )
            return cursor.rowcount

    def get_all_linked_discord_targets(self) -> List[Dict[str, Any]]:
        """Mengambil seluruh tautan Discord yang terdaftar di bot."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM user_discord_links ORDER BY id DESC;")
            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Operasi SKEM Monitor
    # =========================================================================

    def upsert_skem_monitor(self, user_id: int, nrp: str, portal: str = "auto") -> None:
        """Mendaftarkan NRP untuk dimonitor oleh user."""
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO skem_monitors (user_id, nrp, portal, last_seen_json, created_at, updated_at)
                VALUES (?, ?, ?, '[]', ?, ?)
                ON CONFLICT(user_id, nrp) DO UPDATE SET
                    portal = excluded.portal,
                    updated_at = excluded.updated_at;
                """,
                (user_id, nrp.strip(), portal.strip().lower(), now_str, now_str),
            )

    def get_all_skem_monitors(self) -> List[Dict[str, Any]]:
        """Mengambil semua NRP yang sedang dimonitor beserta user_id-nya."""
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM skem_monitors ORDER BY id;")
            return [dict(row) for row in cursor.fetchall()]

    def get_skem_monitors_for_user(self, user_id: int) -> List[Dict[str, Any]]:
        """Mengambil daftar NRP yang dimonitor oleh user tertentu."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM skem_monitors WHERE user_id = ? ORDER BY id;",
                (user_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_skem_last_seen(self, monitor_id: int, last_seen_json: str) -> None:
        """Update last_seen_json untuk entry monitor SKEM tertentu."""
        now_str = datetime.now().isoformat()
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE skem_monitors SET last_seen_json = ?, updated_at = ? WHERE id = ?;",
                (last_seen_json, now_str, monitor_id),
            )

    def remove_skem_monitor(self, user_id: int, nrp: str) -> int:
        """Menghapus NRP dari daftar monitor untuk user tertentu."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM skem_monitors WHERE user_id = ? AND nrp = ?;",
                (user_id, nrp.strip()),
            )
            return cursor.rowcount

    # =========================================================================
    # Operasi Tracking Tugas yang Dikumpulkan (Submitted Tasks)
    # =========================================================================

    def is_user_tasks_seeded(self, user_id: int) -> bool:
        """Cek apakah baseline tugas yang sudah dikumpulkan pernah diinisialisasi untuk user ini."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT tasks_seeded FROM users WHERE user_id = ?;", (user_id,)).fetchone()
            return bool(row["tasks_seeded"]) if row and "tasks_seeded" in row.keys() else False

    def set_user_tasks_seeded(self, user_id: int, seeded: bool = True) -> None:
        """Menandai bahwa baseline riwayat tugas sudah diinisialisasi."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE users SET tasks_seeded = ? WHERE user_id = ?;",
                (1 if seeded else 0, user_id),
            )

    def is_task_submission_tracked(self, user_id: int, task_id: str) -> bool:
        """Cek apakah tugas tertentu sudah pernah dicatat status pengumpulannya."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM submitted_task_tracking WHERE user_id = ? AND task_id = ?;",
                (user_id, str(task_id)),
            ).fetchone()
            return row is not None

    def record_submitted_task(
        self,
        user_id: int,
        task_id: str,
        submission_time: str = "",
        submission_file: str = "",
    ) -> None:
        """Mencatat tugas yang sudah dikumpulkan ke database."""
        now = time.time()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO submitted_task_tracking (user_id, task_id, submission_time, submission_file, created_at)
                VALUES (?, ?, ?, ?, ?);
                """,
                (user_id, str(task_id), str(submission_time), str(submission_file), now),
            )

    # =========================================================================
    # Operasi Tracking Item Akademik Permanen (Tugas Baru & Materi Baru)
    # =========================================================================

    def has_known_items_seeded(self, user_id: int, item_type: str) -> bool:
        """Cek apakah user sudah memiliki minimal 1 item tercatat untuk tipe tertentu."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM known_academic_items WHERE user_id = ? AND item_type = ? LIMIT 1;",
                (user_id, str(item_type)),
            ).fetchone()
            return row is not None

    def is_known_item(self, user_id: int, item_type: str, item_id: str) -> bool:
        """Cek apakah tugas/materi dengan ID tersebut sudah pernah diproses."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM known_academic_items WHERE user_id = ? AND item_type = ? AND item_id = ?;",
                (user_id, str(item_type), str(item_id)),
            ).fetchone()
            return row is not None

    def mark_known_item(self, user_id: int, item_type: str, item_id: str) -> None:
        """Menandai 1 item tugas/materi sebagai sudah dikenal."""
        now = time.time()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO known_academic_items (user_id, item_type, item_id, created_at)
                VALUES (?, ?, ?, ?);
                """,
                (user_id, str(item_type), str(item_id), now),
            )

    def mark_known_items_bulk(self, user_id: int, item_type: str, item_ids: List[str]) -> None:
        """Menandai banyak item tugas/materi sekaligus dalam satu transaksi."""
        if not item_ids:
            return
        now = time.time()
        rows = [(user_id, str(item_type), str(iid), now) for iid in item_ids]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO known_academic_items (user_id, item_type, item_id, created_at)
                VALUES (?, ?, ?, ?);
                """,
                rows,
            )
