#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# PSBOT-001 (PENS Smart Academic Assistant Bot) - Termux (Android) Setup Script
# ==============================================================================
set -e

echo "=========================================================="
echo "      PSBOT-001 Setup Script untuk Termux (Android)       "
echo "=========================================================="

# 1. Update Termux Package Repositories
echo "[1/5] Memperbarui repositori paket Termux..."
pkg update -y && pkg upgrade -y

# 2. Install Python, Node.js, Chromium, and compilation tools
echo "[2/5] Memasang paket sistem (Python, Node.js, Chromium)..."
pkg install -y python nodejs git libjpeg-turbo libpng binutils clang make libcrypt libffi openssl chromium

# 3. Upgrade pip & Install Python packages
echo "[3/5] Memasang dependensi Python..."
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# 4. Install Node.js Baileys Bridge
echo "[4/5] Memasang paket Node.js WhatsApp..."
npm install --omit=dev

# 5. Setup file konfigurasi .env
echo "[5/5] Memeriksa konfigurasi .env..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode('utf-8'))")
    sed -i "s/YOUR_GENERATED_FERNET_KEY_HERE/$KEY/g" .env
    echo "Kunci enkripsi FERNET_KEY otomatis digenerate ke dalam .env."
fi

echo ""
echo "=========================================================="
echo "          Instalasi di Termux Selesai!                    "
echo "=========================================================="
echo "Langkah selanjutnya:"
echo "1. Buka file .env dan masukkan BOT_TOKEN Telegram Anda:"
echo "   nano .env"
echo "2. Konfigurasi preferensi sinkronisasi di config.json (opsional):"
echo "   nano config.json"
echo "3. Jalankan bot dengan:"
echo "   python main.py"
echo "=========================================================="
