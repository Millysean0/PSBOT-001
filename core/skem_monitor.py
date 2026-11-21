"""Modul Monitor SKEM (Sistem Kredit Kegiatan Mahasiswa) PENS.

Scraping data dari portal monitoring SKEM Looker Studio:
1. PJJ: https://pens.id/skem-monitor-pjj
2. Reguler: https://datastudio.google.com/reporting/46a9b1ac-1c35-4cfb-9404-a90fddbb1059/page/p_karkepf7xd

Menggunakan Playwright headless browser untuk navigasi, input NRP di kolom pencarian,
dan mengekstrak data profil serta riwayat kegiatan mahasiswa secara akurat.
Mendukung mahasiswa PJJ maupun Reguler secara otomatis.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from utils.logger import setup_logger

logger = setup_logger(__name__)

SKEM_PORTALS = {
    "pjj": "https://pens.id/skem-monitor-pjj",
    "reguler": "https://datastudio.google.com/reporting/46a9b1ac-1c35-4cfb-9404-a90fddbb1059/page/p_karkepf7xd",
}
SKEM_TIMEOUT_MS = 30000


def _check_playwright_available() -> bool:
    """Cek apakah playwright tersedia di sistem Python."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        return False


def parse_skem_text(lines: List[str], target_nrp: str) -> Optional[Dict[str, Any]]:
    """Parse data profil dan tabel aktivitas SKEM dari baris teks halaman Looker Studio."""
    nama = "-"
    nrp = str(target_nrp)
    angkatan = "-"
    prodi = "-"
    jumlah_kegiatan = "0"
    nilai_kredit = "0"

    # Cek apakah ada data mahasiswa
    has_target_nrp = any(str(target_nrp) in l for l in lines)
    if not has_target_nrp:
        if any("tidak ada data" in l.lower() for l in lines[:25]):
            return None

    # 1. Ekstrak profil mahasiswa
    for i, l in enumerate(lines):
        if l == "Nama" and i + 1 < len(lines):
            nama = lines[i + 1]
        elif l == "Program Studi" and i + 1 < len(lines):
            prodi = lines[i + 1]
        elif l == "Angkatan" and i + 1 < len(lines):
            angkatan = lines[i + 1]
        elif l == "Jumlah Kegiatan" and i + 1 < len(lines):
            jumlah_kegiatan = lines[i + 1]
        elif l == "Nilai Kredit" and i + 1 < len(lines):
            val = lines[i + 1]
            if val not in ("Keterangan", "▼", "Tanggal", "Kegiatan", "Tingkat"):
                nilai_kredit = "0" if val == "Tidak ada data" else val

    # 2. Ekstrak riwayat kegiatan dari tabel (pola: "1.", "2.", dst)
    aktivitas: List[Dict[str, str]] = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(\d+)\.$", lines[i])
        if m:
            nomor = m.group(1)
            chunk: List[str] = []
            j = i + 1
            while j < len(lines) and not re.match(r"^\d+\.$", lines[j]) and "1 - " not in lines[j] and lines[j] != "Nama":
                chunk.append(lines[j])
                j += 1

            if len(chunk) >= 5:
                tgl = chunk[0] if len(chunk) > 0 else "-"
                keg = chunk[1] if len(chunk) > 1 else "-"
                tingkat = chunk[2] if len(chunk) > 2 else "-"
                partisipasi = chunk[3] if len(chunk) > 3 else "-"
                validasi = chunk[4] if len(chunk) > 4 else "Menunggu"
                kredit_act = chunk[5] if len(chunk) > 5 else "-"
                ket = chunk[6] if len(chunk) > 6 else "-"

                aktivitas.append({
                    "nomor": nomor,
                    "tanggal": tgl,
                    "kegiatan": keg,
                    "tingkat": tingkat,
                    "partisipasi": partisipasi,
                    "validasi": validasi,
                    "nilai_kredit": kredit_act,
                    "keterangan": ket,
                })
            i = j
        else:
            i += 1

    return {
        "nama": nama,
        "nrp": nrp,
        "angkatan": angkatan,
        "prodi": prodi,
        "jumlah_kegiatan": jumlah_kegiatan,
        "nilai_kredit": nilai_kredit,
        "aktivitas": aktivitas,
    }


def _scrape_portal(url: str, nrp: str) -> Optional[Dict[str, Any]]:
    """Helper untuk menjalankan Playwright dan scraping Looker Studio untuk satu URL tertentu."""
    if not _check_playwright_available():
        logger.error("Playwright tidak terpasang. Jalankan: pip install playwright && playwright install chromium")
        return None

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-extensions",
                "--disable-sync",
                "--disable-translate",
                "--disable-default-apps",
                "--mute-audio",
                "--no-first-run",
                "--safebrowsing-disable-auto-update",
            ]

            is_termux = os.path.exists("/data/data/com.termux")
            chromium_path = "/data/data/com.termux/files/usr/bin/chromium-browser" if is_termux else None

            if chromium_path and os.path.exists(chromium_path):
                browser = p.chromium.launch(executable_path=chromium_path, args=launch_args, headless=True)
            else:
                browser = p.chromium.launch(args=launch_args, headless=True)

            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="id-ID",
                java_script_enabled=True,
            )

            page = context.new_page()
            page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,mp3}", lambda route: route.abort())
            page.route("**googletagmanager**", lambda route: route.abort())
            page.route("**google-analytics**", lambda route: route.abort())
            page.route("**doubleclick**", lambda route: route.abort())

            try:
                page.goto(url, timeout=SKEM_TIMEOUT_MS, wait_until="domcontentloaded")
                time.sleep(2.5)

                try:
                    page.wait_for_selector("input", timeout=10000)
                except Exception:
                    pass

                inputs = page.query_selector_all("input")
                target_input = None
                for inp in inputs:
                    try:
                        ph = (inp.get_attribute("placeholder") or "").lower()
                        if "nilai" in ph or "masukkan" in ph or "nrp" in ph:
                            target_input = inp
                            break
                    except Exception:
                        continue

                if not target_input and len(inputs) > 0:
                    target_input = inputs[0]

                if not target_input:
                    browser.close()
                    return None

                target_input.click()
                target_input.fill("")
                target_input.fill(str(nrp).strip())
                page.keyboard.press("Enter")

                time.sleep(3.0)

                raw_text = page.evaluate("() => document.body.innerText")
                browser.close()

                if not raw_text:
                    return None

                lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
                return parse_skem_text(lines, nrp)

            except Exception as exc:
                logger.error("[SKEM] Error scraping URL %s untuk NRP %s: %s", url, nrp, exc)
                try:
                    browser.close()
                except Exception:
                    pass
                return None

    except Exception as exc:
        logger.error("[SKEM] Fatal error Playwright: %s", exc)
        return None


def fetch_skem_data(nrp: str, portal_choice: str = "auto") -> Optional[Dict[str, Any]]:
    """Mengambil data SKEM untuk NRP mahasiswa.
    
    Mendukung jalur PJJ dan Reguler.
    Jika portal_choice == 'auto', mencari di portal PJJ terlebih dahulu,
    lalu otomatis mencoba portal Reguler jika di PJJ tidak ada data.
    """
    clean_nrp = str(nrp).strip()
    choice = portal_choice.lower().strip()

    if choice == "pjj":
        logger.info("[SKEM] Mencari data NRP %s di portal PJJ...", clean_nrp)
        res = _scrape_portal(SKEM_PORTALS["pjj"], clean_nrp)
        if res:
            res["portal"] = "PJJ"
        return res

    elif choice in ("reguler", "regular"):
        logger.info("[SKEM] Mencari data NRP %s di portal Reguler...", clean_nrp)
        res = _scrape_portal(SKEM_PORTALS["reguler"], clean_nrp)
        if res:
            res["portal"] = "Reguler"
        return res

    # Mode 'auto': Coba PJJ dulu
    logger.info("[SKEM-Auto] Memeriksa portal PJJ untuk NRP: %s...", clean_nrp)
    res_pjj = _scrape_portal(SKEM_PORTALS["pjj"], clean_nrp)
    if res_pjj and res_pjj.get("nama") and res_pjj.get("nama") != "-":
        res_pjj["portal"] = "PJJ"
        return res_pjj

    # Fallback ke Reguler jika di PJJ kosong
    logger.info("[SKEM-Auto] NRP %s tidak ada di PJJ, beralih ke portal Reguler...", clean_nrp)
    res_reg = _scrape_portal(SKEM_PORTALS["reguler"], clean_nrp)
    if res_reg and res_reg.get("nama") and res_reg.get("nama") != "-":
        res_reg["portal"] = "Reguler"
        return res_reg

    return None


def format_skem_profile(data: Dict[str, Any], is_html: bool = False) -> str:
    """Memformat profil SKEM mahasiswa menjadi teks ramah pengguna."""
    nama = data.get("nama", "-")
    nrp = data.get("nrp", "-")
    angkatan = data.get("angkatan", "-")
    prodi = data.get("prodi", "-")
    portal_type = data.get("portal", "PENS")
    jumlah = data.get("jumlah_kegiatan", "0")
    kredit = data.get("nilai_kredit", "0")
    if kredit in ("Tidak ada data", "Keterangan", "-"):
        kredit = "0"

    aktivitas = data.get("aktivitas", [])

    if is_html:
        res = (
            f"📊 <b>Profil SKEM Mahasiswa ({html.escape(portal_type)})</b>\n\n"
            f"<b>Nama:</b> {html.escape(nama)}\n"
            f"<b>NRP:</b> <code>{html.escape(nrp)}</code>\n"
            f"<b>Angkatan:</b> {html.escape(angkatan)}\n"
            f"<b>Program Studi:</b> {html.escape(prodi)}\n"
            f"<b>Jumlah Kegiatan:</b> {html.escape(str(jumlah))}\n"
            f"<b>Total Nilai Kredit:</b> {html.escape(str(kredit))}\n"
        )
        if aktivitas:
            res += "\n📋 <b>Daftar Riwayat Ajuan Kegiatan:</b>\n"
            for act in aktivitas:
                v = act.get("validasi", "Menunggu")
                status_icon = "✅" if v in ("Disetujui", "Diterima") else ("❌" if v == "Ditolak" else "⏳")
                res += (
                    f"\n<b>{act.get('nomor', '•')}. {html.escape(act.get('kegiatan', '-'))}</b>\n"
                    f"   • Tanggal: {html.escape(act.get('tanggal', '-'))}\n"
                    f"   • Tingkat: {html.escape(act.get('tingkat', '-'))} ({html.escape(act.get('partisipasi', '-'))})\n"
                    f"   • Status: {status_icon} <b>{html.escape(v)}</b>"
                )
                if act.get("keterangan") and act.get("keterangan") != "-":
                    res += f"\n   • Keterangan: <i>{html.escape(act.get('keterangan'))}</i>"
        else:
            res += "\n<i>Belum ada riwayat ajuan kegiatan SKEM yang tercatat.</i>"
    else:
        res = (
            f"📊 *Profil SKEM Mahasiswa ({portal_type})*\n\n"
            f"*Nama:* {nama}\n"
            f"*NRP:* {nrp}\n"
            f"*Angkatan:* {angkatan}\n"
            f"*Program Studi:* {prodi}\n"
            f"*Jumlah Kegiatan:* {jumlah}\n"
            f"*Total Nilai Kredit:* {kredit}\n"
        )
        if aktivitas:
            res += "\n📋 *Daftar Riwayat Ajuan Kegiatan:*\n"
            for act in aktivitas:
                v = act.get("validasi", "Menunggu")
                status_icon = "✅" if v in ("Disetujui", "Diterima") else ("❌" if v == "Ditolak" else "⏳")
                res += (
                    f"\n*{act.get('nomor', '•')}. {act.get('kegiatan', '-')}*\n"
                    f"   • Tanggal: {act.get('tanggal', '-')}\n"
                    f"   • Tingkat: {act.get('tingkat', '-')} ({act.get('partisipasi', '-')})\n"
                    f"   • Status: {status_icon} *{v}*"
                )
                if act.get("keterangan") and act.get("keterangan") != "-":
                    res += f"\n   • Keterangan: _{act.get('keterangan')}_"
        else:
            res += "\n_Belum ada riwayat ajuan kegiatan SKEM yang tercatat._"

    return res


def detect_skem_changes(
    nrp: str,
    old_data_json: str,
    new_data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Mendeteksi perubahan status SKEM antara data lama dan data baru."""
    try:
        old_list: List[Dict] = json.loads(old_data_json) if old_data_json else []
    except Exception:
        old_list = []

    new_list: List[Dict] = new_data.get("aktivitas", [])

    def make_key(entry: Dict) -> str:
        return f"{entry.get('tanggal', '')}|{entry.get('kegiatan', '')}"

    old_map = {make_key(e): e for e in old_list}

    changes = []
    for entry in new_list:
        key = make_key(entry)
        old_entry = old_map.get(key)

        if old_entry is None:
            changes.append({**entry, "change_type": "new"})
        else:
            old_validasi = old_entry.get("validasi", "").lower()
            new_validasi = entry.get("validasi", "").lower()
            if old_validasi != new_validasi:
                changes.append({**entry, "change_type": "status_changed"})

    return changes


# FORMAT NOTIFIKASI TIDAK DIUBAH SAMA SEKALI SESUAI INSTRUKSI USER:
def format_skem_notification(nama: str, nrp: str, entry: Dict[str, Any]) -> Tuple[str, str]:
    """Membuat pesan notifikasi keputusan SKEM (Diterima / Ditolak) sesuai New.txt."""
    validasi = entry.get("validasi", "").lower()
    kegiatan = entry.get("kegiatan", "-")
    tanggal = entry.get("tanggal", "-")
    nilai_kredit = entry.get("nilai_kredit", "-")
    keterangan = entry.get("keterangan", "-")

    if validasi in ("disetujui", "diterima"):
        tg_msg = (
            "🎉 <b>SKEM Kamu diterima !!!</b>\n\n"
            f"<b>Nama:</b> {html.escape(nama)}\n"
            f"<b>NRP:</b> <code>{html.escape(nrp)}</code>\n"
            f"<b>Kegiatan:</b> {html.escape(kegiatan)}\n"
            f"<b>Tanggal:</b> {html.escape(tanggal)}\n"
            f"<b>Nilai SKEM keseluruhan:</b> {html.escape(str(nilai_kredit))}"
        )
        wa_msg = (
            "🎉 *SKEM Kamu diterima !!!*\n\n"
            f"*Nama:* {nama}\n"
            f"*NRP:* {nrp}\n"
            f"*Kegiatan:* {kegiatan}\n"
            f"*Tanggal:* {tanggal}\n"
            f"*Nilai SKEM keseluruhan:* {nilai_kredit}"
        )
    else:
        tg_msg = (
            "⚠️ <b>SKEM Kamu ditolak !!!</b>\n\n"
            f"<b>Nama:</b> {html.escape(nama)}\n"
            f"<b>NRP:</b> <code>{html.escape(nrp)}</code>\n"
            f"<b>Kegiatan:</b> {html.escape(kegiatan)}\n"
            f"<b>Tanggal:</b> {html.escape(tanggal)}\n"
            f"<b>Keterangan:</b> {html.escape(keterangan)}"
        )
        wa_msg = (
            "⚠️ *SKEM Kamu ditolak !!!*\n\n"
            f"*Nama:* {nama}\n"
            f"*NRP:* {nrp}\n"
            f"*Kegiatan:* {kegiatan}\n"
            f"*Tanggal:* {tanggal}\n"
            f"*Keterangan:* {keterangan}"
        )

    return tg_msg, wa_msg


def format_new_skem_notification(nama: str, nrp: str, entry: Dict[str, Any]) -> Tuple[str, str]:
    """Format pesan notifikasi untuk ajuan SKEM baru yang terdeteksi."""
    kegiatan = entry.get("kegiatan", "-")
    tanggal = entry.get("tanggal", "-")
    tingkat = entry.get("tingkat", "-")
    validasi = entry.get("validasi", "Menunggu")

    tg_msg = (
        "📋 <b>Form Ajuan SKEM Baru Terdeteksi !!!</b>\n\n"
        f"<b>Nama:</b> {html.escape(nama)}\n"
        f"<b>NRP:</b> <code>{html.escape(nrp)}</code>\n"
        f"<b>Kegiatan:</b> {html.escape(kegiatan)}\n"
        f"<b>Tanggal:</b> {html.escape(tanggal)}\n"
        f"<b>Tingkat:</b> {html.escape(tingkat)}\n"
        f"<b>Status:</b> {html.escape(validasi)}"
    )
    wa_msg = (
        "📋 *Form Ajuan SKEM Baru Terdeteksi !!!*\n\n"
        f"*Nama:* {nama}\n"
        f"*NRP:* {nrp}\n"
        f"*Kegiatan:* {kegiatan}\n"
        f"*Tanggal:* {tanggal}\n"
        f"*Tingkat:* {tingkat}\n"
        f"*Status:* {validasi}"
    )
    return tg_msg, wa_msg
