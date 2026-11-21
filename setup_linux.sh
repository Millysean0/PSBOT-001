#!/usr/bin/env bash
# ==============================================================================
# PSBOT-001 (PENS Smart Academic Assistant Bot) - Linux Setup Script
# ==============================================================================
set -e

echo "=========================================================="
echo "   PSBOT-001 Installation & Setup (Debian / Ubuntu / Linux)"
echo "=========================================================="

# 1. Update package manager & install system dependencies
echo "[1/5] Memperbarui sistem & memasang dependensi paket..."
if command -v apt-get &> /dev/null; then
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-pip python3-venv nodejs npm libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2
elif command -v dnf &> /dev/null; then
    sudo dnf install -y python3 python3-pip nodejs npm nss atk at-spi2-atk cups-libs libdrm libxkbcommon libXcomposite libXdamage libXfixes libXrandr mesa-libgbm alsa-lib
else
    echo "Distro Linux tidak teridentifikasi secara otomatis. Pastikan Python 3, Node.js, dan pustaka Chromium terpasang."
fi

# 2. Setup Python Virtual Environment
echo "[2/5] Menyiapkan Python Virtual Environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip

# 3. Install Python Dependencies
echo "[3/5] Memasang dependensi Python..."
pip install -r requirements.txt

# 4. Install Playwright Headless Chromium (untuk scraping SKEM Looker Studio)
echo "[4/5] Memasang Playwright headless browser..."
playwright install chromium
playwright install-deps chromium || true

# 5. Install Node.js Dependencies (WhatsApp Baileys Bridge)
echo "[5/5] Memasang dependensi Node.js WhatsApp Bridge..."
npm install --omit=dev

# 6. Template .env jika belum ada
if [ ! -f ".env" ]; then
    echo "Menyiapkan file .env..."
    cp .env.example .env
    
    # Generate Fernet key otomatis
    KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode('utf-8'))")
    sed -i "s/YOUR_GENERATED_FERNET_KEY_HERE/$KEY/g" .env
    echo "Kunci enkripsi FERNET_KEY otomatis dibuat dan disimpan di .env."
fi

echo ""
echo "=========================================================="
echo "   Instalasi Selesai!"
echo "=========================================================="
echo "Langkah selanjutnya:"
echo "1. Buka dan sesuaikan file .env (masukkan BOT_TOKEN dari @BotFather)."
echo "2. Sesuaikan fitur di config.json jika ingin mengaktifkan/menonaktifkan WhatsApp atau auto-download."
echo "3. Jalankan bot dengan perintah:"
echo "   source venv/bin/activate"
echo "   python3 main.py"
echo "=========================================================="
