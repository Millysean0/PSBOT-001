"""Modul kriptografi AES Fernet untuk enkripsi kredensial pengguna."""

from __future__ import annotations

import os
from cryptography.fernet import Fernet


def get_fernet(key: str | None = None) -> Fernet:
    """Membuat instance Fernet dari kunci."""
    fernet_key = key or os.getenv("FERNET_KEY", "")
    if not fernet_key:
        raise ValueError("FERNET_KEY tidak ditemukan di environment.")
    return Fernet(fernet_key.encode("utf-8") if isinstance(fernet_key, str) else fernet_key)


def encrypt_string(plain_text: str, key: str | None = None) -> str:
    """Enkripsi teks biasa menjadi string aman."""
    if not plain_text:
        return ""
    f = get_fernet(key)
    return f.encrypt(plain_text.encode("utf-8")).decode("utf-8")


def decrypt_string(cipher_text: str, key: str | None = None) -> str:
    """Dekripsi ciphertext kembali menjadi teks biasa."""
    if not cipher_text:
        return ""
    f = get_fernet(key)
    return f.decrypt(cipher_text.encode("utf-8")).decode("utf-8")


if __name__ == "__main__":
    new_key = Fernet.generate_key().decode("utf-8")
    print("\n" + "=" * 50)
    print("Kunci FERNET_KEY Baru:")
    print(new_key)
    print("=" * 50 + "\n")
