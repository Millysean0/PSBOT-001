"""ETHOL PENS HTTP Client Multi-User dengan Autentikasi CAS SSO (login.pens.ac.id) & REST API ETHOL.

Mengotomatisasi interaksi dengan portal https://ethol.pens.ac.id menggunakan:
1. EEPIS Central Authentication Service (CAS) SSO pada https://login.pens.ac.id/cas/login
2. REST API terpusat ETHOL (https://ethol.pens.ac.id/api) dengan token JWT Bearer & refresh token
3. requests.Session pool terisolasi per user_id, validasi token otomatis & auto-relogin
4. Scraping & integrasi data mata kuliah, jadwal online, detail matkul, tugas kuliah,
   history presensi, RPS, kontrak kuliah, serta auto-absen kehadiran mahasiswa.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin
import concurrent.futures

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

from utils.logger import setup_logger

logger = setup_logger(__name__)


class EtholClient:
    """Client HTTP untuk interaksi dengan portal akademik ETHOL PENS via CAS SSO & REST API."""

    def __init__(self, config_path: str = "config.json") -> None:
        """Inisialisasi EtholClient dengan konfigurasi dari config.json."""
        self.config_path = config_path
        self.config: Dict[str, Any] = self._load_config()
        self.ethol_cfg = self.config.get("ethol", {})
        self.selectors = self.ethol_cfg.get("selectors", {})

        # Konfigurasi URL utama (CAS SSO & ETHOL Portal)
        self.base_url: str = self.ethol_cfg.get("base_url", "https://ethol.pens.ac.id").rstrip("/")
        self.api_url: str = f"{self.base_url}/api"
        self.login_url: str = self.ethol_cfg.get(
            "login_url",
            f"https://login.pens.ac.id/cas/login?service={self.base_url}%2Fapi%2Fauth%2Fcas-callback",
        )
        self.cas_service_url: str = self.ethol_cfg.get(
            "cas_service_url",
            f"{self.base_url}/api/auth/cas-callback",
        )
        self.dashboard_url: str = self.ethol_cfg.get("dashboard_url", f"{self.base_url}/dashboard")
        self.tugas_url: str = self.ethol_cfg.get("tugas_url", f"{self.base_url}/tugas")
        self.materi_url: str = self.ethol_cfg.get("materi_url", f"{self.base_url}/materi")
        self.presensi_url: str = self.ethol_cfg.get("presensi_url", f"{self.base_url}/presensi")
        self.kuliah_url: str = self.ethol_cfg.get("kuliah_url", f"{self.base_url}/kuliah")

        # Konfigurasi retry & timeout
        self.timeout: int = self.ethol_cfg.get("request_timeout_seconds", 15)
        self.retry_attempts: int = self.ethol_cfg.get("retry_attempts", 3)
        self.retry_backoff: int = self.ethol_cfg.get("retry_backoff_seconds", 5)

        # Session Pool per user_id {user_id: requests.Session}
        self.sessions: Dict[int, requests.Session] = {}
        # Token JWT per user_id {user_id: token_str}
        self.user_tokens: Dict[int, str] = {}
        # Refresh token per user_id {user_id: refresh_token_str}
        self.user_refresh_tokens: Dict[int, str] = {}
        # Profil mahasiswa per user_id {user_id: {"nomor": int, "nipnrp": str, "nama": str, ...}}
        self.user_profiles: Dict[int, Dict[str, Any]] = {}
        # Cache daftar matkul per user_id {user_id: [courses]}
        self.course_cache: Dict[int, List[Dict[str, Any]]] = {}
        self.course_cache_time: Dict[int, float] = {}
        # Cache tugas per user_id {user_id: [tasks]}
        self.task_cache: Dict[int, List[Dict[str, Any]]] = {}
        self.task_cache_time: Dict[int, float] = {}
        # Konfigurasi tahun & semester akademik aktif {tahun_aktif, semester_aktif, ...}
        self.academic_cfg: Dict[str, Any] = {}

        self.user_agent = self.ethol_cfg.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )

    def _load_config(self) -> Dict[str, Any]:
        """Membaca file konfigurasi config.json."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Gagal membaca %s (%s). Menggunakan konfigurasi default.", self.config_path, exc)
            return {}

    def reload_config(self) -> None:
        """Memuat ulang konfigurasi dari file disk."""
        self.config = self._load_config()
        self.ethol_cfg = self.config.get("ethol", {})
        self.selectors = self.ethol_cfg.get("selectors", {})

    def get_session(self, user_id: int) -> requests.Session:
        """Mendapatkan atau membuat instance requests.Session terisolasi untuk user_id tertentu."""
        if user_id not in self.sessions:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": self.user_agent,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "id,en-US;q=0.9,en;q=0.8",
                    "Connection": "keep-alive",
                }
            )
            # Pool adapter teroptimasi untuk performa multi-thread dan polling frekuensi tinggi
            adapter = HTTPAdapter(
                pool_connections=25,
                pool_maxsize=25,
                max_retries=0,
                pool_block=False,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self.sessions[user_id] = session
        return self.sessions[user_id]

    def clear_session(self, user_id: int) -> None:
        """Menghapus session, token, dan cache user dari memory (saat user logout)."""
        if user_id in self.sessions:
            try:
                self.sessions[user_id].cookies.clear()
                self.sessions[user_id].close()
            except Exception:
                pass
            del self.sessions[user_id]

        self.user_tokens.pop(user_id, None)
        self.user_refresh_tokens.pop(user_id, None)
        self.user_profiles.pop(user_id, None)
        self.course_cache.pop(user_id, None)
        self.course_cache_time.pop(user_id, None)
        self.task_cache.pop(user_id, None)
        self.task_cache_time.pop(user_id, None)

    def _request_with_retry(
        self,
        method: str,
        url: str,
        user_id: int = 0,
        **kwargs: Any,
    ) -> Optional[requests.Response]:
        """Melakukan HTTP request dengan retry dan backoff untuk session pengguna."""
        raw_timeout = kwargs.pop("timeout", self.timeout)
        if isinstance(raw_timeout, (int, float)):
            timeout = (min(10.0, float(raw_timeout)), max(float(raw_timeout), 12.0))
        else:
            timeout = raw_timeout

        max_retries = kwargs.pop("max_retries", self.retry_attempts)
        backoff = kwargs.pop("backoff", self.retry_backoff)
        silent_error = kwargs.pop("silent_error", False)

        session = self.get_session(user_id)

        for attempt in range(1, max_retries + 1):
            try:
                response = session.request(method=method, url=url, timeout=timeout, **kwargs)
                return response
            except (requests.exceptions.RequestException, requests.exceptions.Timeout) as exc:
                if not silent_error:
                    if attempt == max_retries and max_retries == 1:
                        logger.debug(
                            "[User %s] Network timeout saat %s %s: %s",
                            user_id,
                            method,
                            url,
                            exc,
                        )
                    else:
                        logger.warning(
                            "[User %s] Network error saat %s %s (Percobaan %d/%d): %s",
                            user_id,
                            method,
                            url,
                            attempt,
                            max_retries,
                            exc,
                        )
                if attempt < max_retries:
                    time.sleep(min(backoff, 1.0))
                else:
                    if not silent_error and max_retries > 1:
                        logger.error("[User %s] Semua percobaan (%d) gagal untuk %s %s", user_id, max_retries, method, url)
        return None

    def _api_request(
        self,
        method: str,
        endpoint: str,
        user_id: int,
        **kwargs: Any,
    ) -> Optional[requests.Response]:
        """Melakukan request ke ETHOL REST API (/api/...) dengan token JWT Bearer & auto-refresh."""
        url = endpoint if endpoint.startswith("http") else f"{self.api_url}{endpoint}"

        headers = kwargs.pop("headers", {})
        token = self.user_tokens.get(user_id)
        if not token:
            # Cek cookie session
            session = self.get_session(user_id)
            token = session.cookies.get("token")
            if token:
                self.user_tokens[user_id] = token

        if token:
            headers["Authorization"] = f"Bearer {token}"
        headers.setdefault("Accept", "application/json, text/plain, */*")

        resp = self._request_with_retry(method, url, user_id=user_id, headers=headers, **kwargs)

        # Cek jika token expired (401 atau 403)
        if resp is not None and resp.status_code in (401, 403):
            ref_ok = self.refresh_token(user_id)
            if ref_ok:
                new_token = self.user_tokens.get(user_id)
                if new_token:
                    headers["Authorization"] = f"Bearer {new_token}"
                resp = self._request_with_retry(method, url, user_id=user_id, headers=headers, **kwargs)
            else:
                # Sesi / token benar-benar kedaluwarsa, bersihkan cache agar auto-relogin dapat berjalan
                self.user_tokens.pop(user_id, None)
                self.user_profiles.pop(user_id, None)

        return resp

    def login(self, user_id: int, username: str, password: str) -> Tuple[bool, str]:
        """Melakukan autentikasi login via EEPIS Central Authentication Service (CAS SSO).

        Alur:
        1. GET halaman CAS login
        2. Ekstrak tiket form (lt, execution, _eventId)
        3. POST data kredensial ke CAS
        4. Ikuti redirect ke callback ETHOL (/api/auth/cas-callback) yang menerbitkan JWT token
        5. Validasi token dan ambil profil mahasiswa
        """
        logger.info("[User %s] Memulai proses login CAS SSO untuk: %s", user_id, username)
        self.clear_session(user_id)
        session = self.get_session(user_id)

        # 1. Buka halaman login CAS
        resp_login = self._request_with_retry("GET", self.login_url, user_id=user_id, allow_redirects=True)
        if not resp_login:
            return False, "Gagal menghubungi server login CAS PENS (Network timeout)."

        soup = BeautifulSoup(resp_login.content, "lxml")
        form = soup.find("form")
        if not form:
            return False, "Tidak dapat menemukan form login pada halaman CAS PENS."

        action = form.get("action") or ""
        post_url = urljoin(resp_login.url, action) if action else self.login_url

        payload: Dict[str, str] = {}
        for hidden in form.find_all("input", type="hidden"):
            name = hidden.get("name")
            if name:
                payload[name] = hidden.get("value", "")

        if "_eventId" not in payload:
            payload["_eventId"] = "submit"
        if "submit" not in payload:
            payload["submit"] = "LOGIN"

        payload["username"] = username.strip()
        payload["password"] = password

        headers = {
            "Referer": resp_login.url,
            "Origin": "https://login.pens.ac.id",
        }

        # 2. Kirim POST kredensial ke CAS (tidak auto redirect agar dapat menangani 302 CAS -> ETHOL secara eksplisit)
        resp_post = self._request_with_retry(
            "POST",
            post_url,
            user_id=user_id,
            data=payload,
            headers=headers,
            allow_redirects=False,
        )

        if not resp_post:
            return False, "Koneksi terputus saat memverifikasi kredensial CAS."

        # Evaluasi redirect
        loc = resp_post.headers.get("Location")
        if resp_post.status_code in (301, 302) and loc:
            # Redirect ke ethol callback
            resp_callback = self._request_with_retry("GET", loc, user_id=user_id, allow_redirects=True)
        elif resp_post.status_code == 200:
            # Kemungkinan gagal login di CAS (form dikembalikan dengan error)
            soup_err = BeautifulSoup(resp_post.content, "lxml")
            err_node = soup_err.select_one(self.selectors.get("cas_error", ".errors, #status, .status-error, .alert-danger, #msg.errors"))
            err_msg = err_node.get_text(strip=True) if err_node else "Kredensial tidak valid di CAS PENS."
            logger.warning("[User %s] Login CAS ditolak: %s", user_id, err_msg)
            return False, err_msg
        else:
            resp_callback = resp_post

        # 3. Ekstrak token JWT dari cookies sesi
        token = session.cookies.get("token")
        refresh_token = session.cookies.get("refresh_token")

        if not token and resp_callback and "token" in resp_callback.cookies:
            token = resp_callback.cookies.get("token")
        if not refresh_token and resp_callback and "refresh_token" in resp_callback.cookies:
            refresh_token = resp_callback.cookies.get("refresh_token")

        if token:
            self.user_tokens[user_id] = token
        if refresh_token:
            self.user_refresh_tokens[user_id] = refresh_token

        # 4. Validasi token dan ambil profil mahasiswa
        profile = self.get_user_profile(user_id)
        if not profile or not profile.get("nomor"):
            return False, "Gagal mendapatkan token sesi portal ETHOL dari CAS callback."

        # Muat konfigurasi akademik aktif
        self.fetch_academic_config(user_id)

        mhs_name = profile.get("nama", username)
        nrp = profile.get("nipnrp", "")
        logger.info("[User %s] Login berhasil untuk mahasiswa: %s (NRP: %s)", user_id, mhs_name, nrp)
        return True, f"Login berhasil sebagai {mhs_name} (NRP: {nrp})"

    def refresh_token(self, user_id: int) -> bool:
        """Melakukan refresh token JWT menggunakan refreshToken."""
        ref_token = self.user_refresh_tokens.get(user_id)
        if not ref_token:
            session = self.get_session(user_id)
            ref_token = session.cookies.get("refresh_token")

        if not ref_token:
            return False

        url = f"{self.api_url}/auth/refresh"
        resp = self._request_with_retry("POST", url, user_id=user_id, json={"refreshToken": ref_token})
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                if data.get("sukses"):
                    session = self.get_session(user_id)
                    new_token = session.cookies.get("token")
                    if new_token:
                        self.user_tokens[user_id] = new_token
                    new_ref = session.cookies.get("refresh_token")
                    if new_ref:
                        self.user_refresh_tokens[user_id] = new_ref
                    self.user_profiles[user_id] = data
                    logger.info("[User %s] Token JWT ETHOL berhasil diperbarui via refresh token.", user_id)
                    return True
            except Exception:
                pass
        return False

    def is_authenticated(self, user_id: int) -> bool:
        """Memeriksa apakah sesi pengguna saat ini masih aktif via validasi token JWT."""
        profile = self.get_user_profile(user_id)
        if profile and profile.get("nomor"):
            return True
        # Coba refresh
        if self.refresh_token(user_id):
            return True
        return False

    def ensure_session(self, user_id: int, username: Optional[str] = None, password: Optional[str] = None) -> bool:
        """Memastikan session user tetap aktif; jalankan refresh token atau re-login jika expired."""
        if not self.user_tokens.get(user_id):
            if username and password:
                logger.info("[User %s] Sesi ETHOL belum aktif / expired. Menjalankan auto re-login CAS...", user_id)
                success, _ = self.login(user_id, username, password)
                return success
            return False

        if self.is_authenticated(user_id):
            return True

        if self.refresh_token(user_id):
            return True

        if not username or not password:
            return False

        logger.info("[User %s] Sesi ETHOL expired. Menjalankan auto re-login CAS...", user_id)
        success, _ = self.login(user_id, username, password)
        return success

    def get_user_profile(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Mengambil data profil mahasiswa pengguna dari /api/auth/validasi-token."""
        if user_id in self.user_profiles:
            return self.user_profiles[user_id]

        resp = self._api_request("GET", "/auth/validasi-token", user_id=user_id)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                if isinstance(data, dict) and data.get("nomor"):
                    self.user_profiles[user_id] = data
                    return data
            except Exception:
                pass
        return None

    def fetch_academic_config(self, user_id: int) -> Dict[str, Any]:
        """Mengambil konfigurasi tahun dan semester aktif dari /api/auth/config."""
        if self.academic_cfg and "tahun_aktif" in self.academic_cfg:
            return self.academic_cfg

        resp = self._api_request("GET", "/auth/config", user_id=user_id)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    self.academic_cfg = data
                    return data
            except Exception:
                pass

        # Fallback default semester berjalan
        now = datetime.now()
        fallback = {
            "tahun_aktif": now.year,
            "semester_aktif": 1 if now.month >= 8 else 2,
            "tahun_ajaran_aktif": f"{now.year}/{now.year + 1}",
        }
        self.academic_cfg = fallback
        return fallback

    def fetch_matkul(self, user_id: int, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Mengambil daftar seluruh mata kuliah pengguna pada semester aktif beserta jadwal online.

        Menggabungkan data dari:
        - GET /api/kuliah?tahun={tahun}&semester={semester}
        - GET /api/jadwal/jadwal-online?tahun={tahun}&semester={semester}

        Returns:
            List of course dictionaries with keys:
            nomor, nama, kode, dosen, nip_dosen, nomor_dosen, kode_kelas,
            pararel, jenis_schema, kuliah_asal, hari, jam, ruang, jadwal_display
        """
        now_ts = time.time()
        if not force_refresh and user_id in self.course_cache:
            if now_ts - self.course_cache_time.get(user_id, 0) < 300:
                return self.course_cache[user_id]

        cfg = self.fetch_academic_config(user_id)
        tahun = cfg.get("tahun_aktif", datetime.now().year)
        semester = cfg.get("semester_aktif", 1)

        # 1. Ambil daftar mata kuliah
        resp_kuliah = self._api_request("GET", "/kuliah", user_id=user_id, params={"tahun": tahun, "semester": semester})
        if not resp_kuliah or resp_kuliah.status_code != 200:
            logger.warning("[User %s] Gagal mengambil daftar kuliah: status %s", user_id, getattr(resp_kuliah, "status_code", None))
            return self.course_cache.get(user_id, [])

        try:
            raw_courses = resp_kuliah.json()
        except Exception:
            raw_courses = []

        if not isinstance(raw_courses, list):
            raw_courses = []

        # 2. Ambil jadwal online perkuliahan
        resp_jadwal = self._api_request("GET", "/jadwal/jadwal-online", user_id=user_id, params={"tahun": tahun, "semester": semester})
        sched_map: Dict[int, Dict[str, Any]] = {}
        if resp_jadwal and resp_jadwal.status_code == 200:
            try:
                raw_schedules = resp_jadwal.json()
                if isinstance(raw_schedules, list):
                    for s in raw_schedules:
                        if isinstance(s, dict) and "kuliah" in s:
                            sched_map[s["kuliah"]] = s
            except Exception:
                pass

        # 3. Gabungkan dan bentuk struktur mata kuliah
        courses: List[Dict[str, Any]] = []
        for idx, item in enumerate(raw_courses, start=1):
            if not isinstance(item, dict):
                continue

            nomor = item.get("nomor", idx)
            mk_data = item.get("matakuliah") or {}
            nama_mk = mk_data.get("nama") or item.get("nama") or f"Mata Kuliah #{idx}"
            kode_mk = str(mk_data.get("nomor") or item.get("kode_kelas") or f"MK-{nomor}")

            # Format nama dosen lengkap dengan gelar
            dosen_nama = item.get("dosen") or "-"
            gelar_dpn = item.get("gelar_dpn")
            gelar_blk = item.get("gelar_blk")

            full_dosen = dosen_nama
            if gelar_dpn:
                full_dosen = f"{gelar_dpn.strip()} {full_dosen}"
            if gelar_blk:
                full_dosen = f"{full_dosen}, {gelar_blk.strip()}"

            nip = item.get("nip_dosen") or "-"
            nomor_dosen = item.get("nomor_dosen")
            kelas = item.get("kode_kelas") or "-"
            pararel = item.get("pararel") or ""
            jenis_schema = item.get("jenisSchema", 2)
            kuliah_asal = item.get("kuliah_asal")

            # Data jadwal
            sc = sched_map.get(nomor, {})
            hari = sc.get("hari") or "-"
            jam_awal = sc.get("jam_awal") or ""
            jam_akhir = sc.get("jam_akhir") or ""
            jam = f"{jam_awal} - {jam_akhir}".strip(" -") if (jam_awal or jam_akhir) else "-"
            ruang = sc.get("ruang") or "-"

            if hari != "-" and jam != "-":
                jadwal_display = f"{hari} ({jam})"
            elif hari != "-":
                jadwal_display = hari
            else:
                jadwal_display = "-"

            courses.append(
                {
                    "nomor": nomor,
                    "nama": nama_mk,
                    "kode": kode_mk,
                    "dosen": full_dosen,
                    "nip_dosen": nip,
                    "nomor_dosen": nomor_dosen,
                    "kode_kelas": kelas,
                    "pararel": pararel,
                    "jenis_schema": jenis_schema,
                    "kuliah_asal": kuliah_asal,
                    "hari": hari,
                    "jam": jam,
                    "ruang": ruang,
                    "jadwal_display": jadwal_display,
                }
            )

        self.course_cache[user_id] = courses
        self.course_cache_time[user_id] = now_ts
        return courses

    def get_detail_matkul(self, user_id: int, target_mk: Any) -> Optional[Dict[str, Any]]:
        """Mengambil detail lengkap mata kuliah berdasarkan nomor atau nama matkul.

        Mengumpulkan informasi sesuai layout antarmuka resmi ETHOL (lihat screenshot):
        - Info Mata Kuliah (Nama, NIP, Nama Dosen, Jadwal & Ruang)
        - Presensi Terakhir Dosen (/api/presensi/terakhir-kuliah)
        - Status Presensi Aktif (/api/presensi/aktif-kuliah)
        - History Presensi Saya (/api/presensi/riwayat)
        - Dokumen Perkuliahan (Kontrak Perkuliahan & RPS via /api/kontrak)
        - Sesi Conference ETHOL

        Args:
            user_id: Telegram user ID.
            target_mk: Nomor kuliah (int) atau nama/kode mata kuliah (str).

        Returns:
            Dict berisi detail lengkap matkul atau None jika tidak ditemukan.
        """
        courses = self.fetch_matkul(user_id)
        if not courses:
            return None

        # Temukan mata kuliah yang cocok
        selected_course: Optional[Dict[str, Any]] = None
        target_str = str(target_mk).strip().lower()

        # Cek nomor ID langsung
        for c in courses:
            if str(c.get("nomor")) == target_str or str(c.get("kode")) == target_str:
                selected_course = c
                break

        # Jika belum cocok, cari berdasarkan substring nama matkul
        if not selected_course:
            for c in courses:
                if target_str in c.get("nama", "").lower():
                    selected_course = c
                    break

        if not selected_course:
            return None

        kuliah_id = selected_course["nomor"]
        jenis_schema = selected_course.get("jenis_schema", 2)
        profile = self.get_user_profile(user_id)
        user_nomor = profile.get("nomor") if profile else None

        # 1. Presensi Terakhir Dosen & Status Open
        presensi_terakhir_str = "-"
        is_presensi_open = False
        presensi_key = ""
        resp_terakhir = self._api_request(
            "GET",
            "/presensi/terakhir-kuliah",
            user_id=user_id,
            params={"kuliah": kuliah_id, "jenis_schema": jenis_schema},
        )
        if resp_terakhir and resp_terakhir.status_code == 200:
            try:
                data_t = resp_terakhir.json()
                if isinstance(data_t, dict) and data_t.get("ditemukan"):
                    presensi_terakhir_str = data_t.get("tanggal_format") or data_t.get("tanggal") or "-"
                    is_presensi_open = bool(data_t.get("open"))
                    presensi_key = data_t.get("key") or ""
            except Exception:
                pass

        # 2. Presensi Aktif untuk mahasiswa saat ini
        active_session = None
        resp_aktif = self._api_request(
            "GET",
            "/presensi/aktif-kuliah",
            user_id=user_id,
            params={"kuliah": kuliah_id, "jenis_schema": jenis_schema},
        )
        if resp_aktif and resp_aktif.status_code == 200:
            try:
                data_a = resp_aktif.json()
                if isinstance(data_a, list) and data_a:
                    active_session = data_a[0]
                    is_presensi_open = True
                    if not presensi_key and isinstance(active_session, dict):
                        presensi_key = active_session.get("key", "")
            except Exception:
                pass

        # 3. History Presensi Saya
        history_presensi: List[Dict[str, Any]] = []
        if user_nomor:
            resp_hist = self._api_request(
                "GET",
                "/presensi/riwayat",
                user_id=user_id,
                params={"kuliah": kuliah_id, "jenis_schema": jenis_schema, "nomor": user_nomor},
            )
            if resp_hist and resp_hist.status_code == 200:
                try:
                    data_h = resp_hist.json()
                    if isinstance(data_h, list):
                        history_presensi = data_h
                except Exception:
                    pass

        # 4. Dokumen Perkuliahan (Kontrak Perkuliahan & RPS)
        kontrak_disetujui = False
        rps_nama = "-"
        rps_url = ""
        params_k = {"kuliah": kuliah_id, "jenis_schema": jenis_schema}
        if user_nomor:
            params_k["mahasiswa"] = user_nomor

        resp_k = self._api_request("GET", "/kontrak", user_id=user_id, params=params_k)
        if resp_k and resp_k.status_code == 200:
            try:
                data_k = resp_k.json()
                if isinstance(data_k, dict):
                    kontrak_disetujui = bool(data_k.get("sudahSetuju"))
                    rps_url = data_k.get("urlRps") or ""
                    rps_nama = data_k.get("namaFile") or ""
                    if rps_nama:
                        rps_nama = re.sub(r"_\d{10,}(?=\.[^.]+$)", "", rps_nama)
            except Exception:
                pass

        return {
            "course": selected_course,
            "presensi_terakhir": presensi_terakhir_str,
            "is_presensi_open": is_presensi_open,
            "presensi_key": presensi_key,
            "active_session": active_session,
            "history_presensi": history_presensi,
            "total_kehadiran": len(history_presensi),
            "kontrak_disetujui": kontrak_disetujui,
            "rps_nama": rps_nama if rps_nama else "Belum tersedia",
            "rps_url": rps_url,
        }

    def fetch_tugas(
        self,
        user_id: int,
        include_all: bool = False,
        target_kuliah: Optional[int] = None,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """Mengambil daftar tugas kuliah pengguna dari API ETHOL (/api/tugas).

        Args:
            user_id: Telegram user ID.
            include_all: Jika True, kembalikan semua tugas termasuk yang sudah dikumpulkan.
                         Jika False, hanya tugas aktif yang belum dikumpulkan.
            target_kuliah: Nomor kuliah spesifik jika hanya ingin mengambil tugas 1 mata kuliah.
            force_refresh: Jika True, abaikan cache tugas dan ambil data segar dari server.

        Returns:
            List of task dictionaries.
        """
        now_ts = time.time()
        # Gunakan cache tugas jika ada dan belum kedaluwarsa (< 180 detik)
        if not force_refresh and not target_kuliah and user_id in self.task_cache:
            if now_ts - self.task_cache_time.get(user_id, 0) < 180:
                cached_tasks = self.task_cache[user_id]
                if not include_all:
                    return [t for t in cached_tasks if not t.get("is_submitted")]
                return cached_tasks

        courses = self.fetch_matkul(user_id)
        if not courses:
            return []

        if target_kuliah:
            courses_to_query = [c for c in courses if c.get("nomor") == target_kuliah]
        else:
            courses_to_query = courses

        all_tasks: List[Dict[str, Any]] = []

        def _fetch_course_tasks(c: Dict[str, Any]) -> List[Dict[str, Any]]:
            kuliah_id = c["nomor"]
            schema = c.get("jenis_schema", 2)
            mk_name = c.get("nama", "Mata Kuliah")

            # Per-kuliah timeout 8s & max 1 retry dengan silent_error agar tidak membuat log gaduh
            resp = self._api_request(
                "GET",
                "/tugas",
                user_id=user_id,
                params={"kuliah": kuliah_id, "jenisSchema": schema},
                timeout=8,
                max_retries=1,
                silent_error=True,
            )
            if not resp or resp.status_code != 200:
                return []

            try:
                task_items = resp.json()
            except Exception:
                return []

            if not isinstance(task_items, list):
                return []

            c_tasks: List[Dict[str, Any]] = []
            for t in task_items:
                if not isinstance(t, dict):
                    continue

                t_id = t.get("id")
                judul = t.get("title") or t.get("judul") or f"Tugas #{t_id}"
                deskripsi = t.get("description") or t.get("deskripsi") or ""

                deadline_str = t.get("deadline") or ""
                deadline_indo = t.get("deadline_indonesia") or deadline_str

                submission_file = (
                    t.get("submission_file")
                    or t.get("file_jawaban")
                    or t.get("file_submission")
                    or t.get("jawaban")
                    or t.get("path_jawaban")
                    or t.get("file_upload")
                )
                submission_time = (
                    t.get("submission_time")
                    or t.get("waktu_kumpul")
                    or t.get("tanggal_kumpul")
                    or t.get("submitted_at")
                )
                submission_time_indo = (
                    t.get("submission_time_indonesia")
                    or t.get("waktu_kumpul_indonesia")
                    or submission_time
                )
                catatan_jawaban = (
                    t.get("submission_note")
                    or t.get("catatan")
                    or t.get("note")
                    or t.get("keterangan")
                    or ""
                )

                is_submitted = bool(submission_time or submission_file)
                grade = t.get("grade")
                tutup = bool(t.get("tutup", 0))

                # Hitung sisa waktu deadline
                hours_left, is_overdue = self._calculate_hours_left(deadline_str)

                # Filter status
                if not include_all and is_submitted:
                    continue

                # Kumpulkan lampiran file dari dosen
                files: List[Dict[str, str]] = []
                for f_item in t.get("file") or []:
                    if isinstance(f_item, dict) and f_item.get("path"):
                        p = f_item["path"]
                        fname = Path(p).name
                        fname_clean = re.sub(r"_\d{10,}(?=\.[^.]+$)", "", fname)
                        files.append({"nama": fname_clean, "url": p})

                # Kumpulkan file jawaban yang dikumpulkan mahasiswa
                submitted_files: List[Dict[str, str]] = []
                if submission_file:
                    if isinstance(submission_file, str):
                        fname = Path(submission_file.split("?")[0]).name
                        fname_clean = re.sub(r"_\d{10,}(?=\.[^.]+$)", "", fname)
                        submitted_files.append({"nama": fname_clean or "jawaban_tugas", "url": submission_file})
                    elif isinstance(submission_file, dict):
                        p = submission_file.get("path") or submission_file.get("url") or submission_file.get("file") or ""
                        fname = submission_file.get("nama") or submission_file.get("name") or Path(str(p).split("?")[0]).name or "jawaban_tugas"
                        fname_clean = re.sub(r"_\d{10,}(?=\.[^.]+$)", "", fname)
                        if p:
                            submitted_files.append({"nama": fname_clean, "url": p})
                    elif isinstance(submission_file, list):
                        for sf in submission_file:
                            if isinstance(sf, str):
                                fname = Path(sf.split("?")[0]).name
                                fname_clean = re.sub(r"_\d{10,}(?=\.[^.]+$)", "", fname)
                                submitted_files.append({"nama": fname_clean or "jawaban_tugas", "url": sf})
                            elif isinstance(sf, dict):
                                p = sf.get("path") or sf.get("url") or sf.get("file") or ""
                                fname = sf.get("nama") or sf.get("name") or Path(str(p).split("?")[0]).name or "jawaban_tugas"
                                fname_clean = re.sub(r"_\d{10,}(?=\.[^.]+$)", "", fname)
                                if p:
                                    submitted_files.append({"nama": fname_clean, "url": p})

                c_tasks.append(
                    {
                        "id": f"tugas_{t_id}",
                        "raw_id": t_id,
                        "matkul": mk_name,
                        "kuliah_id": kuliah_id,
                        "dosen": c.get("dosen", "-"),
                        "judul": judul,
                        "deskripsi": deskripsi.strip(),
                        "deadline": deadline_indo,
                        "deadline_raw": deadline_str,
                        "hours_left": hours_left,
                        "is_overdue": is_overdue,
                        "is_submitted": is_submitted,
                        "submission_time": submission_time_indo,
                        "submission_file": submission_file,
                        "submitted_files": submitted_files,
                        "catatan_jawaban": str(catatan_jawaban).strip(),
                        "grade": grade,
                        "tutup": tutup,
                        "files": files,
                        "category": "tugas",
                        "link": f"{self.base_url}/tugas",
                    }
                )
            return c_tasks

        # Ambil tugas seluruh matkul secara paralel multi-thread agar cepat & tidak blocking
        max_workers = min(len(courses_to_query), 6) if courses_to_query else 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_fetch_course_tasks, c) for c in courses_to_query]
            for future in concurrent.futures.as_completed(futures):
                try:
                    c_res = future.result()
                    if c_res:
                        all_tasks.extend(c_res)
                except Exception:
                    pass

        # Urutkan tugas: yang belum dikumpulkan & deadline terdekat didahulukan
        all_tasks.sort(key=lambda x: (x["is_submitted"], x["hours_left"] if not x["is_overdue"] else 999999))
        if not target_kuliah:
            self.task_cache[user_id] = all_tasks
            self.task_cache_time[user_id] = now_ts

        if not include_all:
            return [t for t in all_tasks if not t.get("is_submitted")]
        return all_tasks

    def _calculate_hours_left(self, deadline_str: str) -> Tuple[int, bool]:
        """Menghitung selisih jam dari sekarang ke batas deadline."""
        if not deadline_str:
            return 9999, False

        parsed_dt = self._parse_datetime(deadline_str)
        if not parsed_dt:
            return 9999, False

        diff = parsed_dt - datetime.now()
        hours = int(diff.total_seconds() // 3600)
        is_overdue = hours < 0
        return hours, is_overdue

    def fetch_deadline_tugas(self, user_id: int, days_threshold: int = 3) -> List[Dict[str, Any]]:
        """Mengambil daftar tugas mendesak pengguna (deadline <= days_threshold hari atau baru saja terlewat)."""
        active_tasks = self.fetch_tugas(user_id=user_id, include_all=False)
        urgent: List[Dict[str, Any]] = []

        max_hours = days_threshold * 24
        for t in active_tasks:
            hours = t.get("hours_left", 9999)
            # Tugas mendekati deadline (0 s.d. max_hours) atau baru terlewat dalam 24 jam terakhir
            if 0 <= hours <= max_hours or (-24 <= hours < 0):
                urgent.append(t)

        urgent.sort(key=lambda x: (x.get("is_overdue", False), x.get("hours_left", 9999)))
        return urgent

    def _parse_datetime(self, text: str) -> Optional[datetime]:
        """Helper parsing format tanggal Indonesia / ISO / format ETHOL."""
        if not text:
            return None

        # Format: 2026-11-30 23:59:00 atau 2026-11-30T23:59:00
        match_iso = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?", text)
        if match_iso:
            y, m, d = int(match_iso.group(1)), int(match_iso.group(2)), int(match_iso.group(3))
            hh = int(match_iso.group(4)) if match_iso.group(4) else 23
            mm = int(match_iso.group(5)) if match_iso.group(5) else 59
            ss = int(match_iso.group(6)) if match_iso.group(6) else 0
            return datetime(y, m, d, hh, mm, ss)

        # Format: 30-11-2026 23:59:00
        match_dmy = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?:[ T](\d{2}):(\d{2}))?", text)
        if match_dmy:
            d, m, y = int(match_dmy.group(1)), int(match_dmy.group(2)), int(match_dmy.group(3))
            hh = int(match_dmy.group(4)) if match_dmy.group(4) else 23
            mm = int(match_dmy.group(5)) if match_dmy.group(5) else 59
            return datetime(y, m, d, hh, mm)

        return None

    def fetch_materi(self, user_id: int) -> List[Dict[str, Any]]:
        """Mengambil daftar materi kuliah pengguna dari seluruh mata kuliah aktif secara paralel."""
        courses = self.fetch_matkul(user_id)
        if not courses:
            return []

        all_materials: List[Dict[str, Any]] = []

        def _fetch_course_materials(c: Dict[str, Any]) -> List[Dict[str, Any]]:
            kuliah_id = c["nomor"]
            schema = c.get("jenis_schema", 2)
            mk_name = c.get("nama", "Kuliah")

            resp = self._api_request(
                "GET",
                "/materi",
                user_id=user_id,
                params={"matakuliah": kuliah_id, "jenis_schema": schema},
                timeout=8,
                max_retries=1,
                silent_error=True,
            )
            if not resp or resp.status_code != 200:
                return []

            try:
                items = resp.json()
            except Exception:
                return []

            if not isinstance(items, list):
                return []

            c_mats: List[Dict[str, Any]] = []
            for m in items:
                if not isinstance(m, dict):
                    continue
                m_id = m.get("id") or m.get("nomor")
                judul = m.get("judul") or m.get("nama") or f"Materi #{m_id}"
                tgl = m.get("tanggal_format") or m.get("tanggal") or "-"
                link_file = m.get("path") or m.get("url") or ""

                c_mats.append(
                    {
                        "id": f"materi_{m_id}",
                        "raw_id": m_id,
                        "matkul": mk_name,
                        "dosen": c.get("dosen", "-"),
                        "judul": judul,
                        "tanggal": tgl,
                        "link": link_file,
                        "category": "materi",
                    }
                )
            return c_mats

        max_workers = min(len(courses), 6) if courses else 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_fetch_course_materials, c) for c in courses]
            for future in concurrent.futures.as_completed(futures):
                try:
                    c_res = future.result()
                    if c_res:
                        all_materials.extend(c_res)
                except Exception:
                    pass

        return all_materials

    def _check_single_course_presensi(self, user_id: int, c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Mengecek status presensi aktif satu mata kuliah dengan timeout cepat dan non-blocking."""
        kuliah_id = c["nomor"]
        schema = c.get("jenis_schema", 2)
        mk_name = c.get("nama", "Mata Kuliah")

        # 1. Cek via /presensi/terakhir-kuliah (timeout 4 detik, 1 retry, silent agar log bersih)
        resp_t = self._api_request(
            "GET",
            "/presensi/terakhir-kuliah",
            user_id=user_id,
            params={"kuliah": kuliah_id, "jenis_schema": schema},
            timeout=4.0,
            max_retries=1,
            silent_error=True,
        )

        is_open = False
        key = ""
        jam = datetime.now().strftime("%H:%M:%S")
        minggu = "-"

        if resp_t and resp_t.status_code == 200:
            try:
                data_t = resp_t.json()
                if isinstance(data_t, dict) and data_t.get("ditemukan"):
                    if data_t.get("open"):
                        is_open = True
                        key = data_t.get("key") or ""
                        minggu_val = data_t.get("minggu") or data_t.get("pertemuan") or "-"
                        minggu = str(minggu_val)
                        jam_val = (
                            data_t.get("jam_buka")
                            or data_t.get("waktu_buka")
                            or data_t.get("created_at")
                            or data_t.get("tanggal")
                        )
                        if jam_val:
                            if "T" in str(jam_val):
                                jam = str(jam_val).split("T")[1][:8]
                            elif " " in str(jam_val):
                                jam = str(jam_val).split(" ")[1][:8]
                            else:
                                jam = str(jam_val)[:8]
            except Exception:
                pass

        # 2. Cek juga via /presensi/aktif-kuliah jika belum terdeteksi open
        if not is_open:
            resp_a = self._api_request(
                "GET",
                "/presensi/aktif-kuliah",
                user_id=user_id,
                params={"kuliah": kuliah_id, "jenis_schema": schema},
                timeout=4.0,
                max_retries=1,
                silent_error=True,
            )
            if resp_a and resp_a.status_code == 200:
                try:
                    data_a = resp_a.json()
                    if isinstance(data_a, list) and data_a:
                        is_open = True
                        if not key and isinstance(data_a[0], dict):
                            key = data_a[0].get("key", "")
                            minggu_val = data_a[0].get("minggu") or data_a[0].get("pertemuan") or "-"
                            minggu = str(minggu_val)
                            jam_val = data_a[0].get("jam_buka") or data_a[0].get("waktu_buka") or data_a[0].get("created_at")
                            if jam_val:
                                if "T" in str(jam_val):
                                    jam = str(jam_val).split("T")[1][:8]
                                elif " " in str(jam_val):
                                    jam = str(jam_val).split(" ")[1][:8]
                                else:
                                    jam = str(jam_val)[:8]
                except Exception:
                    pass

        if is_open and key:
            return {
                "mk_code": str(kuliah_id),
                "mk_name": mk_name,
                "kuliah": kuliah_id,
                "jenis_schema": schema,
                "kuliah_asal": c.get("kuliah_asal"),
                "key": key,
                "dosen": c.get("dosen", "-"),
                "jam": jam,
                "minggu": minggu,
                "category": "presensi",
            }
        return None

    def fetch_presensi_active(self, user_id: int) -> List[Dict[str, Any]]:
        """Mendeteksi sesi presensi yang sedang terbuka untuk diabsen oleh pengguna secara paralel."""
        courses = self.fetch_matkul(user_id)
        if not courses:
            return []

        active_sessions: List[Dict[str, Any]] = []

        max_workers = min(len(courses), 8)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(self._check_single_course_presensi, user_id, c) for c in courses]
            for future in concurrent.futures.as_completed(futures):
                try:
                    res = future.result()
                    if res:
                        active_sessions.append(res)
                except Exception:
                    pass

        return active_sessions

        return active_sessions

    def submit_absen(self, user_id: int, presensi_item: Dict[str, Any]) -> Dict[str, Any]:
        """Melakukan submit presensi kehadiran mahasiswa ke POST /api/presensi/mahasiswa."""
        mk_name = presensi_item.get("mk_name", "Mata Kuliah")
        kuliah_id = presensi_item.get("kuliah")
        jenis_schema = presensi_item.get("jenis_schema", 2)
        key = presensi_item.get("key")
        kuliah_asal = presensi_item.get("kuliah_asal")

        profile = self.get_user_profile(user_id)
        if not profile or not profile.get("nomor"):
            return {
                "success": False,
                "message": "Profil mahasiswa belum terverifikasi.",
                "mk": mk_name,
            }

        user_nomor = profile["nomor"]

        if not key:
            return {
                "success": False,
                "message": "Key sesi presensi tidak ditemukan.",
                "mk": mk_name,
            }

        payload = {
            "kuliah": kuliah_id,
            "jenis_schema": jenis_schema,
            "mahasiswa": user_nomor,
            "key": key,
            "kuliah_asal": kuliah_asal,
        }

        logger.info("[User %s] Mengirim absensi untuk %s (key: %s)", user_id, mk_name, key)
        resp = self._api_request("POST", "/presensi/mahasiswa", user_id=user_id, json=payload)

        if not resp:
            return {
                "success": False,
                "message": "Gagal terhubung ke server saat submit presensi.",
                "mk": mk_name,
            }

        if resp.status_code == 200:
            try:
                res_json = resp.json()
                if res_json.get("sukses"):
                    logger.info("[User %s] Presensi berhasil terkirim untuk %s", user_id, mk_name)
                    return {
                        "success": True,
                        "already_attended": False,
                        "message": f"Presensi berhasil dicatat untuk: {mk_name}",
                        "mk": mk_name,
                    }
                else:
                    pesan = res_json.get("pesan") or "Presensi ditolak sistem."
                    is_already = any(w in pesan.lower() for w in ["sudah", "pernah", "tercatat", "telah"])
                    if is_already:
                        logger.info("[User %s] Mahasiswa sudah presensi sebelumnya untuk %s: %s", user_id, mk_name, pesan)
                        return {
                            "success": True,
                            "already_attended": True,
                            "message": f"Presensi sudah tercatat sebelumnya ({pesan})",
                            "mk": mk_name,
                        }
                    return {
                        "success": False,
                        "already_attended": False,
                        "message": pesan,
                        "mk": mk_name,
                    }
            except Exception:
                return {
                    "success": True,
                    "message": f"Presensi berhasil tercatat untuk: {mk_name}",
                    "mk": mk_name,
                }
        else:
            return {
                "success": False,
                "message": f"Server merespon dengan status code {resp.status_code}",
                "mk": mk_name,
            }

    def download_file(self, user_id: int, file_url: str) -> Optional[Tuple[str, bytes]]:
        """Mengunduh file dari URL ETHOL menggunakan session autentikasi pengguna.

        Args:
            user_id: Telegram user ID (untuk session terautentikasi).
            file_url: URL atau path relatif file (misal '/uploads/materi/xxx.pdf').

        Returns:
            Tuple (filename, bytes) jika berhasil, atau None jika gagal.
        """
        if not file_url:
            return None

        # Bangun URL absolut jika berupa path relatif
        if file_url.startswith("http"):
            url = file_url
        elif file_url.startswith("/"):
            url = f"{self.base_url}{file_url}"
        else:
            url = f"{self.base_url}/{file_url}"

        try:
            resp = self._api_request("GET", url, user_id=user_id, stream=True)
            if not resp or resp.status_code != 200:
                # Coba tanpa auth
                session = self.get_session(user_id)
                resp = session.get(url, timeout=60)
                if not resp or resp.status_code != 200:
                    logger.warning("[User %s] Gagal download file %s (status %s)", user_id, url, resp.status_code if resp else "N/A")
                    return None

            # Ambil nama file dari Content-Disposition header atau URL
            content_disp = resp.headers.get("Content-Disposition", "")
            fname = None
            if content_disp and "filename=" in content_disp:
                import re as _re
                m = _re.search(r'filename[^;=\n]*=([\'"]?)([^\'";\n]+)\1', content_disp)
                if m:
                    fname = m.group(2).strip()

            if not fname:
                fname = Path(url.split("?")[0]).name or "file"

            # Bersihkan nama file dari timestamp panjang
            fname = re.sub(r"_\d{10,}(?=\.[^.]+$)", "", fname)

            content = resp.content
            max_size = 50 * 1024 * 1024  # 50 MB limit
            if len(content) > max_size:
                logger.warning("[User %s] File terlalu besar (%s MB), skip download.", user_id, len(content) // (1024 * 1024))
                return None

            return fname, content

        except Exception as exc:
            logger.error("[User %s] Error download file %s: %s", user_id, url, exc)
            return None

    def fetch_dashboard(self, user_id: int) -> Dict[str, Any]:
        """Mengambil data dashboard dan notifikasi pengguna dari /api/notifikasi/mahasiswa?filterNotif=SEMUA."""
        resp = self._api_request("GET", "/notifikasi/mahasiswa", user_id=user_id, params={"filterNotif": "SEMUA"}, timeout=15, silent_error=True)
        result: Dict[str, Any] = {
            "status": "success" if resp and resp.status_code == 200 else "failed",
            "timestamp": datetime.now().isoformat(),
            "notifications": [],
            "content_hash": "",
        }

        if not resp or resp.status_code != 200:
            return result

        try:
            notifs = resp.json()
        except Exception:
            notifs = []

        if not isinstance(notifs, list):
            notifs = []

        raw_strings: List[str] = []
        for item in notifs:
            if not isinstance(item, dict):
                continue

            n_id = str(item.get("id") or item.get("nomor") or "")
            title = item.get("judul") or item.get("title") or "Pemberitahuan ETHOL"
            body = item.get("isi") or item.get("pesan") or item.get("desc") or ""
            waktu = item.get("tanggal_format") or item.get("tanggal") or item.get("waktu") or datetime.now().strftime("%H:%M")

            notif_item = {
                "id": n_id or hashlib.md5(f"{title}_{body}".encode("utf-8")).hexdigest()[:12],
                "title": title,
                "body": body,
                "time": waktu,
                "category": "notifikasi",
                "raw_text": f"{title} {body}",
            }
            result["notifications"].append(notif_item)
            raw_strings.append(f"{title}:{body}:{waktu}")

        hash_src = "|".join(raw_strings) if raw_strings else "empty"
        result["content_hash"] = hashlib.sha256(hash_src.encode("utf-8")).hexdigest()
        return result

    def inspect_mode(self, user_id: int = 0, pages: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Menyimpan data API / HTML ke logs/debug_*.html untuk kalibrasi."""
        if pages is None:
            pages = ["cas_login", "config", "kuliah", "jadwal", "tugas"]

        debug_dir = Path("logs")
        debug_dir.mkdir(parents=True, exist_ok=True)
        saved_files: List[Dict[str, Any]] = []
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        for page in pages:
            if page == "cas_login":
                resp = self._request_with_retry("GET", self.login_url, user_id=user_id)
            elif page == "config":
                resp = self._api_request("GET", "/auth/config", user_id=user_id)
            elif page == "kuliah":
                cfg = self.fetch_academic_config(user_id)
                resp = self._api_request("GET", "/kuliah", user_id=user_id, params={"tahun": cfg.get("tahun_aktif"), "semester": cfg.get("semester_aktif")})
            elif page == "jadwal":
                cfg = self.fetch_academic_config(user_id)
                resp = self._api_request("GET", "/jadwal/jadwal-online", user_id=user_id, params={"tahun": cfg.get("tahun_aktif"), "semester": cfg.get("semester_aktif")})
            else:
                resp = None

            content = resp.text if resp else "<!-- Tidak ada data -->"
            filename = f"debug_{page}_{timestamp}.json" if page != "cas_login" else f"debug_{page}_{timestamp}.html"
            filepath = debug_dir / filename

            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                size_kb = round(filepath.stat().st_size / 1024, 2)
                saved_files.append(
                    {
                        "page": page,
                        "file": filename,
                        "path": str(filepath.resolve()),
                        "size_kb": size_kb,
                        "status": "success" if resp and resp.status_code == 200 else "failed",
                    }
                )
            except Exception as exc:
                logger.error("Gagal menyimpan debug %s: %s", filepath, exc)

        return saved_files
