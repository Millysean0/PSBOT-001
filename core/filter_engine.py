"""Filter engine untuk memfilter notifikasi, deduplikasi hash 24 jam, dan rate limiting.

Mendukung mode multi-user: menyaring notifikasi berdasarkan preferensi blacklist,
watchlist mata kuliah, dan muted categories per masing-masing pengguna di database SQLite.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from core.database import DatabaseManager
from utils.logger import setup_logger

logger = setup_logger(__name__)


class FilterEngine:
    """Mesin penyaring dan deduplikasi notifikasi ETHOL multi-user."""

    def __init__(self, config_path: str = "config.json") -> None:
        """Inisialisasi FilterEngine dengan konfigurasi dasar."""
        self.config_path = config_path
        self.config = self._load_config()

        filter_cfg = self.config.get("filter", {})
        self.default_keywords_exclude: Set[str] = set(
            k.lower().strip() for k in filter_cfg.get("keywords_exclude", ["batal", "uji coba", "test"])
        )
        self.rate_limit_per_minute: int = int(filter_cfg.get("rate_limit_per_minute", 10))
        self.dedup_ttl_seconds: float = float(filter_cfg.get("dedup_ttl_hours", 24)) * 3600.0

        # Pelacak rate limiting per user: {user_id: [timestamp, ...]}
        self.user_rate_limits: Dict[int, List[float]] = {}

    def _load_config(self) -> Dict[str, Any]:
        """Membaca konfigurasi default dari config.json."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Gagal membaca config filter (%s), menggunakan default.", exc)
            return {}

    def compute_hash(self, item: Dict[str, Any]) -> str:
        """Menghitung hash SHA-256 unik untuk sebuah item notifikasi/tugas/materi."""
        ident = item.get("id") or item.get("link") or ""
        title = item.get("title") or item.get("judul") or ""
        body = item.get("body") or item.get("matkul") or ""
        raw_str = f"{ident}_{title}_{body}"
        return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()

    def check_user_rate_limit(self, user_id: int) -> bool:
        """Memeriksa apakah pengiriman notifikasi ke user masih di bawah limit per menit."""
        now = time.time()
        timestamps = self.user_rate_limits.setdefault(user_id, [])
        # Bersihkan timestamp yang lebih dari 60 detik lalu
        self.user_rate_limits[user_id] = [ts for ts in timestamps if (now - ts) < 60.0]

        if len(self.user_rate_limits[user_id]) >= self.rate_limit_per_minute:
            logger.warning("Rate limit tercapai untuk user %d: %d notif dalam 60s.", user_id, len(self.user_rate_limits[user_id]))
            return False

        self.user_rate_limits[user_id].append(now)
        return True

    def should_notify_user(
        self,
        user_id: int,
        item: Dict[str, Any],
        db: DatabaseManager,
    ) -> Tuple[bool, str, bool]:
        """Mengevaluasi apakah suatu notifikasi harus dikirimkan ke user tertentu.

        Args:
            user_id: Telegram user ID.
            item: Objek data notifikasi/tugas/materi.
            db: DatabaseManager instance.

        Returns:
            Tuple[bool, str, bool]: (Boleh dikirim?, Alasan jika di-drop, Apakah item prioritas/watchlist?).
        """
        category = str(item.get("category", "notifikasi")).lower().strip()
        user_filters = db.get_user_filters(user_id)
        muted_set = user_filters.get("muted", set())
        exclude_set = user_filters.get("exclude", set()) | self.default_keywords_exclude
        watchlist_set = user_filters.get("watchlist", set())

        # 1. Cek apakah kategori di-mute oleh pengguna
        if category in muted_set:
            return False, f"Kategori '{category}' sedang di-mute.", False

        # 2. Cek deduplikasi 24 jam via database
        item_hash = self.compute_hash(item)
        if db.is_notification_seen(user_id, item_hash, ttl_seconds=self.dedup_ttl_seconds):
            return False, "Duplikat notifikasi (sudah dikirim dalam 24 jam terakhir).", False

        # Teks pencarian kata kunci
        searchable_text = " ".join(
            str(val) for val in [
                item.get("title"),
                item.get("judul"),
                item.get("body"),
                item.get("matkul"),
                item.get("dosen"),
            ] if val
        ).lower()

        # 3. Cek kata kunci blacklist
        for kw in exclude_set:
            if kw and kw in searchable_text:
                return False, f"Mengandung kata kunci yang diblokir: '{kw}'", False

        # 4. Cek rate limit per user
        if not self.check_user_rate_limit(user_id):
            return False, "Melebihi rate limit (10 notif/menit).", False

        # 5. Cek apakah cocok dengan watchlist mata kuliah prioritas
        is_priority = any(w in searchable_text for w in watchlist_set) if watchlist_set else False

        # Lolos filter -> tandai notifikasi sebagai sudah dilihat di database
        db.mark_notification_seen(user_id, item_hash)
        return True, "Lolos filter.", is_priority
