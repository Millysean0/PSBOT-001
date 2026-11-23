# PSBOT-001: PENS Smart Academic Assistant Bot

<p align="center">
  <img src="https://img.shields.io/badge/Version-1.0.0-blue.svg" alt="Version 1.0.0">
  <img src="https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Node.js-18%2B-green.svg" alt="Node.js 18+">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20Termux-orange.svg" alt="Platform Support">
  <img src="https://img.shields.io/badge/Architecture-Multi--User%20SQLite%20WAL-purple.svg" alt="Architecture">
</p>

**PSBOT-001** adalah bot asisten akademik cerdas berbasis **Telegram** dan **WhatsApp** yang terintegrasi langsung dengan portal pembelajaran **ETHOL PENS** (*EEPIS Online Learning*) dan portal pemantauan **SKEM** (*Sistem Kredit Kegiatan Mahasiswa*) Politeknik Elektronika Negeri Surabaya.

Bot ini dirancang dengan arsitektur multi-user yang hemat sumber daya (RAM < 150 MB), mendukung autentikasi resmi **CAS SSO (login.pens.ac.id)**, enkripsi kredensial **Fernet AES**, dan sinkronisasi lintas platform secara *real-time*.

---

## 🚀 Fitur Utama

### 1. Sinkronisasi Notifier ETHOL (Telegram & WhatsApp)
- **Notifikasi Presensi / Absen Dibuka**: Menampilkan mata kuliah, jam dibuka, dan minggu ke berapa.
- **Notifikasi Materi Baru**: Mengumumkan penambahan materi kuliah dan bahan ajar.
- **Notifikasi Tugas Baru**: Mengabarkan tugas perkuliahan, deskripsi, dan tenggat waktu (*deadline*).
- **Notifikasi Tugas Dikumpulkan**: Mengonfirmasi saat tugas telah berhasil dikumpulkan oleh mahasiswa.
- **Mention `@all` Cerdas**: Tagar `@all` otomatis disematkan hanya ketika notifikasi presensi dikirimkan ke grup WhatsApp, dan ditiadakan pada chat pribadi untuk kenyamanan pengguna.

### 2. Auto-Download Dokumen & File Lampiran
- **File Materi Kuliah**: Otomatis mengunduh dokumen materi (PDF, PPT, DOCX, ZIP) dari ETHOL dan mengirimkannya langsung ke Telegram dan WhatsApp pengguna.
- **Lampiran Soal Tugas**: Mengunduh berkas petunjuk atau lampiran soal tugas saat ada tugas baru.
- **Berkas Tugas yang Dikumpulkan**: Otomatis mengunduh dan mencadangkan file jawaban yang baru saja diunggah ke portal ETHOL.

### 3. Pemantau SKEM Real-time (PJJ & Reguler)
- Mendukung pemantauan riwayat ajuan SKEM untuk **Mahasiswa PJJ** (`pens.id/skem-monitor-pjj`) maupun **Mahasiswa Reguler** (`Google Looker Studio Portal`).
- **Deteksi Otomatis (Auto-Detection)**: Bot secara cerdas mencari NRP mahasiswa di kedua portal portal tanpa perlu input rumit.
- **Notifikasi Status**: Memberikan pembaruan seketika saat form ajuan baru terdeteksi, atau ketika ajuan disetujui/diterima maupun ditolak oleh tim kemahasiswaan.

### 4. Konfigurasi Fleksibel via `config.json`
- Pengaturan non-rahasia dipisahkan dari `.env` ke `config.json`.
- Pengguna dapat mengaktifkan atau menonaktifkan fitur sinkronisasi WhatsApp, auto-download materi, auto-download tugas, dan interval polling hanya dengan mengubah nilai `true`/`false`.

---

## 📁 Struktur Direktori

```text
PSBOT-001/
├── core/
│   ├── __init__.py
│   ├── database.py         # Multi-user SQLite WAL database manager
│   ├── ethol_client.py     # HTTP Client CAS SSO + ETHOL REST API
│   ├── filter_engine.py    # Deduplikasi hash 24 jam & blacklist filter
│   ├── notifier.py         # Dispatcher pesan & file ke Telegram & WhatsApp
│   ├── scheduler.py        # Background task scheduler (APScheduler)
│   ├── skem_monitor.py     # Scraper portal SKEM (PJJ & Reguler via Playwright)
│   └── whatsapp.py         # Python bridge controller ke Baileys
├── handlers/
│   ├── __init__.py
│   ├── callbacks.py        # Inline keyboard query callback router
│   └── commands.py         # Telegram bot command handlers
├── utils/
│   ├── __init__.py
│   ├── crypto.py           # Enkripsi & Dekripsi Fernet (AES)
│   └── logger.py           # Konfigurasi logging aplikasi
├── .env.example            # Template environment variables
├── .gitignore              # Git ignore file
├── config.json             # Pengaturan fitur & parameter polling
├── main.py                 # Titik masuk utama (Entrypoint)
├── package.json            # Dependensi Node.js (Baileys WhatsApp)
├── requirements.txt        # Dependensi Python
├── setup_linux.sh          # Skrip instalasi otomatis Linux / VPS
├── setup_termux.sh         # Skrip instalasi otomatis Android Termux
└── wa_bridge.js            # Node.js Baileys engine untuk WhatsApp
```

---

## 🛠️ Panduan Instalasi & Menjalankan

### Persyaratan Sistem
- **Python**: Versi 3.10 atau yang lebih baru.
- **Node.js**: Versi 18 atau yang lebih baru (diperlukan jika menggunakan sinkronisasi WhatsApp).
- **RAM**: Minimal 512 MB (Sangat ringan).

---

### A. Instalasi di Windows

1. **Clone repositori atau unduh source code**:
   ```powershell
   git clone https://github.com/username/PSBOT-001.git
   cd PSBOT-001
   ```

2. **Buat dan aktifkan Virtual Environment Python**:
   ```powershell
   python -m venv venv
   .\venv\Scripts\activate
   ```

3. **Pasang dependensi Python & Playwright**:
   ```powershell
   pip install -r requirements.txt
   playwright install chromium
   ```

4. **Pasang dependensi Node.js**:
   ```powershell
   npm install --omit=dev
   ```

5. **Siapkan konfigurasi `.env`**:
   Salin file `.env.example` menjadi `.env`:
   ```powershell
   Copy-Item .env.example .env
   ```
   Buka file `.env` dan isi:
   - `BOT_TOKEN`: Token bot Telegram dari [@BotFather](https://t.me/BotFather).
   - `FERNET_KEY`: Buat kunci baru dengan menjalankan:
     ```powershell
     python -m utils.crypto
     ```
     Lalu tempel hasilnya ke baris `FERNET_KEY`.
   - `WA_PHONE_NUMBER`: (Opsional) Nomor WhatsApp dengan format internasional (contoh: `6281234567890`) jika ingin login menggunakan pairing code, atau biarkan kosong untuk login via scan QR code.

6. **Jalankan Bot**:
   ```powershell
   python main.py
   ```

---

### B. Instalasi di Linux / VPS (Ubuntu, Debian)

1. **Unduh dan jalankan skrip instalasi otomatis**:
   ```bash
   git clone https://github.com/username/PSBOT-001.git
   cd PSBOT-001
   chmod +x setup_linux.sh
   ./setup_linux.sh
   ```

2. **Atur file `.env`**:
   ```bash
   nano .env
   ```
   Masukkan `BOT_TOKEN` Telegram Anda. Kunci `FERNET_KEY` sudah otomatis dibuatkan oleh skrip setup.

3. **Jalankan bot**:
   ```bash
   source venv/bin/activate
   python3 main.py
   ```

#### Menjalankan sebagai Layanan Latar Belakang (Systemd)
Agar bot tetap aktif setelah terminal ditutup atau VPS di-restart:
```ini
# /etc/systemd/system/psbot.service
[Unit]
Description=PSBOT-001 Academic Assistant
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/PSBOT-001
ExecStart=/home/ubuntu/PSBOT-001/venv/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
Aktifkan service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable psbot
sudo systemctl start psbot
```

---

### C. Instalasi di Android (Termux)

1. Buka aplikasi Termux dan pastikan repositori sudah mutakhir:
   ```bash
   pkg update -y && pkg install -y git
   ```

2. Clone repositori dan jalankan skrip setup:
   ```bash
   git clone https://github.com/username/PSBOT-001.git
   cd PSBOT-001
   chmod +x setup_termux.sh
   ./setup_termux.sh
   ```

3. Buka dan isi `BOT_TOKEN` pada file `.env`:
   ```bash
   nano .env
   ```

4. Jalankan bot:
   ```bash
   python main.py
   ```

---

## ⚙️ Referensi Pengaturan (`config.json`)

File `config.json` memungkinkan Anda mengatur seluruh perilaku bot tanpa menyentuh kode program:

```json
{
  "bot": {
    "name": "PSBOT-001",
    "version": "1.0.0"
  },
  "features": {
    "whatsapp_sync_enabled": true,
    "auto_download_materi": true,
    "auto_download_tugas": true,
    "auto_download_submitted_task": true,
    "skem_monitor_enabled": true,
    "auto_absen_enabled": true
  },
  "polling": {
    "ethol_poll_interval_seconds": 30,
    "skem_poll_interval_seconds": 300,
    "presensi_cooldown_seconds": 300
  },
  "skem": {
    "portals": {
      "pjj": "https://pens.id/skem-monitor-pjj",
      "reguler": "https://datastudio.google.com/reporting/46a9b1ac-1c35-4cfb-9404-a90fddbb1059/page/p_karkepf7xd"
    }
  }
}
```

| Parameter | Tipe | Keterangan |
|---|---|---|
| `whatsapp_sync_enabled` | `boolean` | Set `false` jika bot hanya ingin dijalankan secara murni di Telegram tanpa WhatsApp. |
| `auto_download_materi` | `boolean` | Otomatis mengunduh dan mengirim berkas materi baru ke chat. |
| `auto_download_tugas` | `boolean` | Otomatis mengunduh dan mengirim berkas lampiran soal tugas baru. |
| `auto_download_submitted_task` | `boolean` | Otomatis mengunduh dan mencadangkan berkas jawaban tugas yang telah dikumpulkan. |
| `skem_monitor_enabled` | `boolean` | Mengaktifkan/menonaktifkan pemantau status SKEM latar belakang. |
| `auto_absen_enabled` | `boolean` | Mengaktifkan/menonaktifkan fitur auto-presensi kehadiran mahasiswa. |
| `ethol_poll_interval_seconds` | `integer` | Interval polling notifikasi ETHOL (default: 30 detik). |
| `skem_poll_interval_seconds` | `integer` | Interval pengecekan status ajuan SKEM (default: 300 detik / 5 menit). |

---

## 💬 Daftar Perintah (Commands)

### Perintah Telegram
| Perintah | Deskripsi |
|---|---|
| `/start` | Membuka menu interaktif dan menampilkan status bot. |
| `/login <email> <pass>` | Login akun PENS melalui CAS SSO (pesan otomatis dihapus demi keamanan). |
| `/logout` | Logout dan menghapus kredensial terenkripsi dari database. |
| `/status` | Melihat status koneksi akun, performa sistem, dan metrik bot. |
| `/absen` | Membuka panel status presensi, riwayat, dan tombol eksekusi cepat. |
| `/tugas` | Menampilkan seluruh tugas aktif dan informasi pengumpulan. |
| `/deadline` | Menampilkan tugas-tugas dengan batas pengumpulan mendesak (≤ 3 hari). |
| `/materi` | Menampilkan daftar materi perkuliahan terbaru. |
| `/matkul` | Menampilkan daftar mata kuliah semester berjalan. |
| `/skem <NRP>` | Mendaftarkan bot untuk terus mengawasi ajuan SKEM suatu NRP. |
| `/skem check <NRP> [pjj/reguler]` | Memeriksa data profil dan riwayat ajuan SKEM mahasiswa secara langsung. |
| `/skem list` | Menampilkan daftar NRP yang saat ini sedang diawasi. |
| `/skem stop <NRP>` | Berhenti mengawasi ajuan SKEM NRP tersebut. |
| `/connect` | Menghasilkan kode unik sementara untuk menautkan akun ke WhatsApp. |
| `/disconnect` | Memutus tautan akun Telegram dari WhatsApp. |
| `/filter` | Menampilkan dan mengatur kata kunci blacklist atau watchlist matkul. |
| `/mute <kategori>` | Membisukan notifikasi kategori tertentu (`tugas`, `materi`, `presensi`). |
| `/unmute <kategori>` | Mengaktifkan kembali notifikasi kategori yang dibisukan. |
| `/pause` | Menjeda background polling akun Anda sementara. |
| `/resume` | Melanjutkan kembali background polling akun. |

### Perintah WhatsApp
Ketik perintah langsung di chat pribadi bot WhatsApp atau di grup yang ditautkan:
| Perintah | Deskripsi |
|---|---|
| `-connect <kode>` | Menautkan chat/grup WhatsApp dengan akun bot Telegram menggunakan kode unik. |
| `-disconnect` | Memutus tautan chat/grup WhatsApp. |
| `-status` | Melihat daftar akun ETHOL yang sedang tertaut di chat ini. |
| `-testnotif` | Memeriksa kelancaran jalur pengiriman pesan WhatsApp bot. |
| `-skem <NRP>` | Mendaftarkan pemantauan otomatis SKEM untuk NRP mahasiswa. |
| `-skemcheck <NRP>` | Mengecek profil dan tabel riwayat ajuan SKEM mahasiswa di portal PENS. |

---

## 🔒 Privasi dan Keamanan Kredensial

1. **Penyimpanan Password**: Password tidak pernah disimpan dalam format teks polos (*plaintext*). Kredensial dienkripsi dengan standar industri kriptografi **AES-128-CBC** melalui pustaka `cryptography.fernet`.
2. **Kunci Enkripsi Independen**: Kunci `FERNET_KEY` unik untuk setiap instalasi bot dan tidak dibagikan ke publik.
3. **Pembersihan Riwayat**: Perintah `/login` yang dikirim pengguna di Telegram langsung dihapus seketika oleh bot guna mencegah kebocoran password pada layar atau riwayat chat.

---

*Disclaimer: Bot ini merupakan utilitas pihak ketiga independen yang memanfaatkan CAS SSO dan REST API resmi ETHOL PENS serta Looker Studio SKEM PENS. Serta developer dari bot ini tidak bertanggung jawab jika akun PENS anda terkena banned dan sejenisnya dan bot ini berfungsi untuk menjadi pengingat absensi bukan sebagai pengganti Kewajiban mahasiswa dalam melakukan absen*