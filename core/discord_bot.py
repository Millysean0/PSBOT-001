"""Modul Integrasi Bot Discord untuk PSBOT-001 (PENS Smart Academic Assistant).

Mendukung Discord Bot API via discord.py (v2.0+):
- Menghubungkan channel atau DM Discord ke akun ETHOL PENS melalui kode !connect.
- Perintah interaktif: !help, !status, !matkul, !tugas, !deadline, !materi, !absen, !login, !connect, !disconnect, !ping.
- Menerima dan menyiarkan notifikasi otomatis dari Notifier (Presensi, Materi baru, Tugas baru, Tugas dikumpulkan).
- Mendukung pengiriman file lampiran tugas & materi langsung ke channel Discord.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from core.database import DatabaseManager
from core.ethol_client import EtholClient
from utils.logger import setup_logger

logger = setup_logger(__name__)


def mask_identifier(identifier: str) -> str:
    """Menyensor alamat email atau NRP untuk privasi tampilan di Discord."""
    if not identifier:
        return "Pengguna"
    text = str(identifier).strip()
    if "@" in text:
        parts = text.split("@", 1)
        local, domain = parts[0], parts[1]
        if len(local) <= 2:
            masked = local[0] + "***"
        elif len(local) <= 4:
            masked = local[0] + "***" + local[-1]
        else:
            masked = local[:2] + "****" + local[-2:]
        return f"{masked}@{domain}"
    if text.isdigit() and len(text) >= 6:
        return text[:3] + "****" + text[-2:]
    if len(text) > 4:
        return text[:2] + "****" + text[-2:]
    return text


class DiscordManager:
    """Manajer Bot Discord untuk PSBOT-001."""

    def __init__(
        self,
        token: str,
        db: DatabaseManager,
        client: Optional[EtholClient] = None,
        default_channel_id: Optional[str] = None,
        command_prefix: str = "!",
        status_activity: str = "ETHOL PENS Academic",
        admin_user_id: Optional[int] = None,
        notifier: Optional[Any] = None,
    ) -> None:
        self.token = token.strip()
        self.db = db
        self.client = client
        self.default_channel_id = str(default_channel_id).strip() if default_channel_id else None
        self.command_prefix = command_prefix or "!"
        self.status_activity = status_activity
        self.admin_user_id = admin_user_id
        self.notifier = notifier

        # Setup Discord Bot client dengan Intents yang tepat
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        self.bot = commands.Bot(
            command_prefix=self.command_prefix,
            intents=intents,
            help_command=None,  # Custom !help
        )
        self._is_running = False
        self._bot_task: Optional[asyncio.Task] = None

        self._register_events()
        self._register_commands()

    @property
    def is_connected(self) -> bool:
        """Cek apakah bot Discord sedang online dan terhubung."""
        return self.bot.is_ready() and not self.bot.is_closed()

    def _resolve_user_id_for_context(self, ctx: commands.Context) -> Optional[int]:
        """Mencari user_id ETHOL yang tertaut dengan channel ini atau DM user ini."""
        # 1. Cari tautan channel ID
        channel_id_str = str(ctx.channel.id)
        user_id = self.db.get_user_by_discord_target(channel_id_str)
        if user_id:
            return user_id

        # 2. Cari tautan user ID Discord
        author_id_str = str(ctx.author.id)
        user_id = self.db.get_user_by_discord_target(author_id_str)
        if user_id:
            return user_id

        return None

    def _register_events(self) -> None:
        """Mendaftarkan event listener Discord."""

        @self.bot.event
        async def on_ready() -> None:
            logger.info("Discord Bot online sebagai %s (ID: %s)", self.bot.user, self.bot.user.id if self.bot.user else "-")
            try:
                activity = discord.Activity(
                    type=discord.ActivityType.watching,
                    name=self.status_activity,
                )
                await self.bot.change_presence(activity=activity)
            except Exception as exc:
                logger.warning("Gagal menyetel activity Discord Bot: %s", exc)

        @self.bot.event
        async def on_command_error(ctx: commands.Context, error: Exception) -> None:
            if isinstance(error, commands.CommandNotFound):
                return
            if isinstance(error, commands.MissingRequiredArgument):
                await ctx.reply(f"⚠️ Argumen perintah kurang. Ketik `{self.command_prefix}help` untuk melihat panduan penggunaan.")
                return
            logger.error("Discord command error [%s]: %s", ctx.command, error)
            await ctx.reply(f"❌ Terjadi kesalahan saat memproses perintah: {error}")

    def _register_commands(self) -> None:
        """Mendaftarkan perintah-perintah Discord."""

        @self.bot.command(name="help", aliases=["bantuan", "h"])
        async def cmd_help(ctx: commands.Context) -> None:
            embed = discord.Embed(
                title="🎓 Panduan Perintah PSBOT-001 (Discord)",
                description=(
                    "PSBOT-001 adalah asisten akademik pintar mahasiswa PENS untuk memantau "
                    "notifikasi ETHOL (Presensi, Materi, Tugas) secara otomatis.\n\n"
                    "Gunakan prefix `" + self.command_prefix + "` sebelum setiap perintah."
                ),
                color=discord.Color.blue(),
            )
            embed.add_field(
                name="🔗 Integrasi Akun",
                value=(
                    f"`{self.command_prefix}connect <kode>` : Tautkan channel/DM ini dengan kode dari bot Telegram (`/connect`)\n"
                    f"`{self.command_prefix}disconnect` : Putus tautan notifikasi dari channel/DM ini\n"
                    f"`{self.command_prefix}login <user> <pass>` : Login akun PENS CAS (Kirim via DM bot saja demi keamanan!)"
                ),
                inline=False,
            )
            embed.add_field(
                name="📚 Akademik & Kuliah",
                value=(
                    f"`{self.command_prefix}status` : Cek profil akun & status notifikasi\n"
                    f"`{self.command_prefix}matkul` : Daftar mata kuliah semester aktif\n"
                    f"`{self.command_prefix}tugas` : Daftar tugas aktif di ETHOL\n"
                    f"`{self.command_prefix}deadline` : Tugas dengan batas waktu mendesak (≤3 hari)\n"
                    f"`{self.command_prefix}materi` : Daftar materi kuliah terbaru"
                ),
                inline=False,
            )
            embed.add_field(
                name="📝 Presensi & Sistem",
                value=(
                    f"`{self.command_prefix}absen` : Cek status & toggle Auto-Absen\n"
                    f"`{self.command_prefix}ping` : Cek latensi bot"
                ),
                inline=False,
            )
            embed.add_field(
                name="🧪 Uji Coba Notifikasi (Test)",
                value=(
                    f"`{self.command_prefix}testmatkul` : Simulasi notifikasi presensi dibuka\n"
                    f"`{self.command_prefix}testabsen` : Simulasi notifikasi presensi berhasil dicatat\n"
                    f"`{self.command_prefix}testmateri` : Simulasi notifikasi materi baru (+ contoh PDF)\n"
                    f"`{self.command_prefix}testtugas` : Simulasi notifikasi tugas baru (+ contoh PDF)"
                ),
                inline=False,
            )
            embed.set_footer(text="PSBOT-001 • PENS Smart Academic Assistant")
            await ctx.reply(embed=embed)

        @self.bot.command(name="ping")
        async def cmd_ping(ctx: commands.Context) -> None:
            latency_ms = round(self.bot.latency * 1000, 2)
            await ctx.reply(f"🏓 Pong! Latensi Discord: `{latency_ms} ms`")

        @self.bot.command(name="connect", aliases=["link", "tautkan"])
        async def cmd_connect(ctx: commands.Context, kode: Optional[str] = None) -> None:
            if not kode:
                embed = discord.Embed(
                    title="🔗 Menautkan Akun ke Discord",
                    description=(
                        "Untuk menghubungkan channel Discord ini dengan notifikasi akun ETHOL Anda:\n\n"
                        "1. Buka bot Telegram PSBOT-001 Anda\n"
                        "2. Ketik perintah `/connect` di Telegram untuk membuat kode tautan sementara\n"
                        f"3. Masukkan kode tersebut di sini: `{self.command_prefix}connect <KODE>`\n\n"
                        "Contoh: `" + self.command_prefix + "connect ETH-9A2F1B`"
                    ),
                    color=discord.Color.gold(),
                )
                await ctx.reply(embed=embed)
                return

            clean_code = kode.strip().upper()
            user_id = self.db.validate_and_consume_wa_connect_code(clean_code)
            if not user_id:
                await ctx.reply(
                    "❌ **Kode tautan tidak valid atau sudah kadaluarsa.**\n"
                    "Silakan buat kode baru melalui perintah `/connect` di bot Telegram."
                )
                return

            target_id = str(ctx.channel.id)
            target_type = "dm" if isinstance(ctx.channel, discord.DMChannel) else "channel"
            guild_id = str(ctx.guild.id) if ctx.guild else ""

            self.db.link_user_discord(
                user_id=user_id,
                discord_target_id=target_id,
                target_type=target_type,
                guild_id=guild_id,
            )

            # Ambil profil user
            user_prof = self.db.get_user(user_id) or {}
            display_name = user_prof.get("nama") or user_prof.get("ethol_username_display") or f"User {user_id}"

            embed = discord.Embed(
                title="✅ Channel Berhasil Ditautkan!",
                description=(
                    f"Channel ini sekarang terhubung dengan akun **{display_name}**.\n\n"
                    "Notifikasi yang akan otomatis dikirimkan ke channel ini:\n"
                    "• ❗ Presensi perkuliahan dibuka (Auto-Absen)\n"
                    "• 📝 Tugas baru beserta file lampiran soal\n"
                    "• 📚 Materi baru beserta file dokumen unduhan\n"
                    "• 📥 Konfirmasi tugas berhasil dikumpulkan\n\n"
                    f"Ketik `{self.command_prefix}status` untuk melihat status akun."
                ),
                color=discord.Color.green(),
            )
            embed.set_footer(text=f"User ID ETHOL: {user_id}")
            await ctx.reply(embed=embed)
            logger.info("Discord target %s (%s) berhasil ditautkan ke user_id %s", target_id, target_type, user_id)

        @self.bot.command(name="disconnect", aliases=["unlink"])
        async def cmd_disconnect(ctx: commands.Context) -> None:
            target_id = str(ctx.channel.id)
            removed = self.db.unlink_by_discord_target_id(target_id)
            if not removed:
                author_id = str(ctx.author.id)
                removed = self.db.unlink_by_discord_target_id(author_id)

            if removed:
                await ctx.reply("✅ Tautan notifikasi Discord berhasil diputus. Channel ini tidak akan lagi menerima notifikasi.")
            else:
                await ctx.reply("ℹ️ Channel/User ini belum tertaut dengan akun notifikasi manapun.")

        @self.bot.command(name="login")
        async def cmd_login(ctx: commands.Context, username: Optional[str] = None, password: Optional[str] = None) -> None:
            # Cegah login di server/channel publik demi keamanan kredensial pengguna
            if not isinstance(ctx.channel, discord.DMChannel):
                try:
                    await ctx.message.delete()
                except Exception:
                    pass
                await ctx.send(
                    f"⚠️ **PERINGATAN KEAMANAN:** Jangan ketik password di channel publik!\n"
                    f"Silakan kirim pesan langsung (DM) ke bot ini: `{self.command_prefix}login <username> <password>`.",
                    delete_after=10,
                )
                return

            if not username or not password:
                await ctx.reply(
                    f"Format login:\n`{self.command_prefix}login <email_pens/netid> <password>`\n\n"
                    "Data login Anda diamankan dengan enkripsi AES Fernet."
                )
                return

            if not self.client:
                await ctx.reply("❌ Klien ETHOL belum siap. Silakan hubungi admin bot.")
                return

            wait_msg = await ctx.reply("⏳ Sedang memverifikasi kredensial ke server PENS CAS SSO...")

            try:
                # Login menggunakan akun discord.author.id sebagai pseudo_user_id jika belum ada akun
                discord_author_id = ctx.author.id
                existing_user_id = self.db.get_user_by_discord_target(str(discord_author_id))
                user_id = existing_user_id or discord_author_id

                user_data = self.db.get_or_create_user(user_id, telegram_username=ctx.author.name, first_name=ctx.author.display_name)
                success, msg = await asyncio.to_thread(self.client.login, user_id, username, password, self.db)

                if success:
                    self.db.link_user_discord(
                        user_id=user_id,
                        discord_target_id=str(discord_author_id),
                        target_type="dm",
                    )
                    prof = self.client.get_user_profile(user_id) or {}
                    nama = prof.get("nama", ctx.author.display_name)
                    nrp = prof.get("nipnrp", "-")

                    embed = discord.Embed(
                        title="✅ Login PENS CAS SSO Berhasil!",
                        description=(
                            f"Selamat datang, **{nama}**!\n\n"
                            f"• **NRP/NIP:** `{nrp}`\n"
                            f"• **Akun:** `{mask_identifier(username)}`\n"
                            f"• **Auto-Absen:** Aktif ✅\n\n"
                            f"Ketik `{self.command_prefix}matkul` untuk melihat jadwal kuliah Anda."
                        ),
                        color=discord.Color.green(),
                    )
                    await wait_msg.edit(content=None, embed=embed)
                else:
                    await wait_msg.edit(content=f"❌ **Login Gagal:** {msg}\nPeriksa kembali username dan password Anda.")
            except Exception as exc:
                logger.error("Error cmd_login discord: %s", exc)
                await wait_msg.edit(content=f"❌ Terjadi kesalahan saat mencoba login: {exc}")

        @self.bot.command(name="status")
        async def cmd_status(ctx: commands.Context) -> None:
            user_id = self._resolve_user_id_for_context(ctx)
            if not user_id:
                await ctx.reply(
                    f"❌ Channel atau akun Discord Anda belum ditautkan ke akun ETHOL.\n"
                    f"Ketik `{self.command_prefix}connect <kode>` untuk menautkan akun."
                )
                return

            user_data = self.db.get_user(user_id)
            if not user_data or not user_data.get("is_logged_in"):
                await ctx.reply("⚠️ Akun belum login ke ETHOL. Silakan login atau perbarui tautan akun.")
                return

            nama = user_data.get("nama") or user_data.get("first_name") or "-"
            nrp = user_data.get("nrp") or "-"
            account_display = user_data.get("ethol_username_display") or "-"
            auto_absen = "Aktif ✅" if user_data.get("auto_absen_enabled") else "Nonaktif ❌"
            is_paused = "Dijeda ⏸️" if user_data.get("is_paused") else "Berjalan Aktif ▶️"

            embed = discord.Embed(
                title="📊 Status Akun PSBOT-001",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Nama Mahasiswa", value=str(nama), inline=True)
            embed.add_field(name="NRP", value=str(nrp), inline=True)
            embed.add_field(name="Akun SSO", value=mask_identifier(account_display), inline=True)
            embed.add_field(name="Auto-Absen", value=auto_absen, inline=True)
            embed.add_field(name="Status Polling", value=is_paused, inline=True)
            embed.add_field(name="ID Pengguna", value=f"`{user_id}`", inline=True)
            embed.set_footer(text="PSBOT-001 • PENS Smart Academic Assistant")
            await ctx.reply(embed=embed)

        @self.bot.command(name="matkul", aliases=["kuliah"])
        async def cmd_matkul(ctx: commands.Context) -> None:
            user_id = self._resolve_user_id_for_context(ctx)
            if not user_id or not self.client:
                await ctx.reply(f"❌ Belum ditautkan. Ketik `{self.command_prefix}connect <kode>` terlebih dahulu.")
                return

            wait_msg = await ctx.reply("⏳ Mengambil daftar mata kuliah dari portal ETHOL...")
            try:
                courses = await asyncio.to_thread(self.client.get_courses, user_id)
                if not courses:
                    await wait_msg.edit(content="ℹ️ Tidak ada mata kuliah aktif di semester ini.")
                    return

                embed = discord.Embed(
                    title="📚 Daftar Mata Kuliah Semester Aktif",
                    description=f"Ditemukan **{len(courses)}** mata kuliah terdaftar:",
                    color=discord.Color.purple(),
                )
                for idx, c in enumerate(courses, 1):
                    mk_name = c.get("nama") or c.get("mata_kuliah") or "Mata Kuliah"
                    dosen = c.get("dosen") or "-"
                    jadwal = c.get("hari") or c.get("jadwal") or "-"
                    embed.add_field(
                        name=f"{idx}. {mk_name}",
                        value=f"👨‍🏫 Dosen: {dosen}\n⏰ Jadwal: {jadwal}",
                        inline=False,
                    )
                embed.set_footer(text="PSBOT-001 • PENS Smart Academic Assistant")
                await wait_msg.edit(content=None, embed=embed)
            except Exception as exc:
                logger.error("Error cmd_matkul discord: %s", exc)
                await wait_msg.edit(content=f"❌ Gagal mengambil daftar mata kuliah: {exc}")

        @self.bot.command(name="tugas", aliases=["pr"])
        async def cmd_tugas(ctx: commands.Context) -> None:
            user_id = self._resolve_user_id_for_context(ctx)
            if not user_id or not self.client:
                await ctx.reply(f"❌ Belum ditautkan. Ketik `{self.command_prefix}connect <kode>` terlebih dahulu.")
                return

            wait_msg = await ctx.reply("⏳ Memeriksa daftar tugas dari portal ETHOL...")
            try:
                courses = await asyncio.to_thread(self.client.get_courses, user_id)
                all_tasks: List[Dict[str, Any]] = []

                for c in courses:
                    mk_id = c.get("id") or c.get("kuliah_id")
                    mk_name = c.get("nama") or c.get("mata_kuliah") or "Mata Kuliah"
                    if not mk_id:
                        continue
                    tasks = await asyncio.to_thread(self.client.get_course_tasks, user_id, mk_id)
                    for t in tasks:
                        t["mk_name"] = mk_name
                        all_tasks.append(t)

                if not all_tasks:
                    await wait_msg.edit(content="🎉 **Hore! Tidak ada tugas aktif saat ini.** Semua tugas telah selesai atau belum ada tugas baru.")
                    return

                embed = discord.Embed(
                    title="📝 Daftar Tugas Kuliah Aktif",
                    description=f"Ditemukan **{len(all_tasks)}** tugas terdaftar:",
                    color=discord.Color.orange(),
                )
                for idx, t in enumerate(all_tasks[:10], 1):
                    judul = t.get("judul") or t.get("nama") or "Tugas"
                    mk = t.get("mk_name", "-")
                    dl = t.get("deadline") or t.get("batas_waktu") or "-"
                    embed.add_field(
                        name=f"{idx}. {judul}",
                        value=f"📖 Matkul: {mk}\n⏰ Deadline: {dl}",
                        inline=False,
                    )
                if len(all_tasks) > 10:
                    embed.set_footer(text=f"Menampilkan 10 dari {len(all_tasks)} tugas • Ketik !deadline untuk tugas mendesak")
                else:
                    embed.set_footer(text="PSBOT-001 • PENS Smart Academic Assistant")

                await wait_msg.edit(content=None, embed=embed)
            except Exception as exc:
                logger.error("Error cmd_tugas discord: %s", exc)
                await wait_msg.edit(content=f"❌ Gagal mengambil daftar tugas: {exc}")

        @self.bot.command(name="deadline")
        async def cmd_deadline(ctx: commands.Context) -> None:
            user_id = self._resolve_user_id_for_context(ctx)
            if not user_id or not self.client:
                await ctx.reply(f"❌ Belum ditautkan. Ketik `{self.command_prefix}connect <kode>` terlebih dahulu.")
                return

            wait_msg = await ctx.reply("⏳ Memfilter tugas dengan deadline mendesak...")
            try:
                courses = await asyncio.to_thread(self.client.get_courses, user_id)
                urgent_tasks: List[Dict[str, Any]] = []

                for c in courses:
                    mk_id = c.get("id") or c.get("kuliah_id")
                    mk_name = c.get("nama") or c.get("mata_kuliah") or "Mata Kuliah"
                    if not mk_id:
                        continue
                    tasks = await asyncio.to_thread(self.client.get_course_tasks, user_id, mk_id)
                    for t in tasks:
                        t["mk_name"] = mk_name
                        urgent_tasks.append(t)

                if not urgent_tasks:
                    await wait_msg.edit(content="🎉 Tidak ada tugas mendesak saat ini!")
                    return

                embed = discord.Embed(
                    title="⏰ Tugas Kuliah dengan Deadline Mendesak",
                    description="Segera selesaikan tugas-tugas berikut:",
                    color=discord.Color.red(),
                )
                for idx, t in enumerate(urgent_tasks[:8], 1):
                    judul = t.get("judul") or "Tugas"
                    mk = t.get("mk_name", "-")
                    dl = t.get("deadline") or "-"
                    embed.add_field(
                        name=f"⚠️ {idx}. {judul}",
                        value=f"📖 Matkul: {mk}\n⏰ Deadline: **{dl}**",
                        inline=False,
                    )
                embed.set_footer(text="PSBOT-001 • PENS Smart Academic Assistant")
                await wait_msg.edit(content=None, embed=embed)
            except Exception as exc:
                logger.error("Error cmd_deadline discord: %s", exc)
                await wait_msg.edit(content=f"❌ Gagal memfilter deadline: {exc}")

        @self.bot.command(name="absen", aliases=["presensi"])
        async def cmd_absen(ctx: commands.Context, mode: Optional[str] = None) -> None:
            user_id = self._resolve_user_id_for_context(ctx)
            if not user_id:
                await ctx.reply(f"❌ Belum ditautkan. Ketik `{self.command_prefix}connect <kode>` terlebih dahulu.")
                return

            if mode and mode.lower() in ("on", "aktif", "1", "true"):
                self.db.set_auto_absen_enabled(user_id, True)
                await ctx.reply("✅ **Auto-Absen diaktifkan.** Bot akan otomatis melakukan presensi saat dosen membuka sesi kuliah.")
            elif mode and mode.lower() in ("off", "mati", "0", "false"):
                self.db.set_auto_absen_enabled(user_id, False)
                await ctx.reply("⏸️ **Auto-Absen dinonaktifkan.** Anda perlu melakukan presensi manual.")
            else:
                user_data = self.db.get_user(user_id) or {}
                status_absen = "Aktif ✅" if user_data.get("auto_absen_enabled") else "Nonaktif ❌"
                embed = discord.Embed(
                    title="📝 Kontrol Auto-Absen",
                    description=(
                        f"Status Auto-Absen saat ini: **{status_absen}**\n\n"
                        f"Perintah pengubah status:\n"
                        f"• `{self.command_prefix}absen on` : Mengaktifkan Auto-Absen otomatis\n"
                        f"• `{self.command_prefix}absen off` : Menonaktifkan Auto-Absen"
                    ),
                    color=discord.Color.teal(),
                )
                await ctx.reply(embed=embed)

        @self.bot.command(name="testmatkul", aliases=["testpresensi"])
        async def cmd_testmatkul(ctx: commands.Context) -> None:
            if self.notifier:
                await self.notifier.send_test_matkul(target_platform="discord", discord_target_id=str(ctx.channel.id))
            else:
                await ctx.reply("❌ Notifier belum siap.")

        @self.bot.command(name="testabsen")
        async def cmd_testabsen(ctx: commands.Context) -> None:
            if self.notifier:
                await self.notifier.send_test_absen(target_platform="discord", discord_target_id=str(ctx.channel.id))
            else:
                await ctx.reply("❌ Notifier belum siap.")

        @self.bot.command(name="testmateri")
        async def cmd_testmateri(ctx: commands.Context) -> None:
            if self.notifier:
                await self.notifier.send_test_materi(target_platform="discord", discord_target_id=str(ctx.channel.id))
            else:
                await ctx.reply("❌ Notifier belum siap.")

        @self.bot.command(name="testtugas")
        async def cmd_testtugas(ctx: commands.Context) -> None:
            if self.notifier:
                await self.notifier.send_test_tugas(target_platform="discord", discord_target_id=str(ctx.channel.id))
            else:
                await ctx.reply("❌ Notifier belum siap.")

    # =========================================================================
    # Pengiriman Notifikasi & Announcement ke Discord
    # =========================================================================

    async def _resolve_channel(self, target_id: str | int) -> Optional[Any]:
        """Menemukan objek channel atau user Discord berdasarkan ID."""
        try:
            target_int = int(target_id)
        except (ValueError, TypeError):
            return None

        # 1. Cari di cache channel
        channel = self.bot.get_channel(target_int)
        if channel:
            return channel

        # 2. Coba fetch channel dari API Discord
        try:
            channel = await self.bot.fetch_channel(target_int)
            if channel:
                return channel
        except Exception:
            pass

        # 3. Cari di cache user / DM
        user = self.bot.get_user(target_int)
        if user:
            return user

        # 4. Coba fetch user dari API Discord
        try:
            user = await self.bot.fetch_user(target_int)
            if user:
                return user
        except Exception:
            pass

        return None

    async def send_announcement(
        self,
        target_id: str | int,
        content: str,
        embed: Optional[discord.Embed] = None,
        is_absen: bool = False,
    ) -> bool:
        """Mengirim pesan atau embed ke target channel atau user Discord."""
        if not self.is_connected:
            return False

        dest = await self._resolve_channel(target_id)
        if not dest:
            logger.warning("[Discord] Target channel/user ID %s tidak ditemukan.", target_id)
            return False

        try:
            send_content = content
            if is_absen and isinstance(dest, discord.TextChannel):
                # Tambahkan mention @everyone jika di channel/server Discord
                if "@everyone" not in send_content:
                    if "@all" in send_content:
                        send_content = send_content.replace("@all", "@everyone")
                    else:
                        send_content = f"{send_content.rstrip()}\n\n@everyone"

            if embed:
                await dest.send(content=send_content if send_content else None, embed=embed)
            else:
                await dest.send(content=send_content)
            return True
        except Exception as exc:
            logger.error("[Discord] Gagal mengirim pesan ke %s: %s", target_id, exc)
            return False

    async def send_file(
        self,
        target_id: str | int,
        file_bytes: bytes,
        filename: str,
        caption: str = "",
    ) -> bool:
        """Mengirim dokumen/file ke channel atau user Discord."""
        if not self.is_connected:
            return False

        dest = await self._resolve_channel(target_id)
        if not dest:
            return False

        try:
            file_obj = discord.File(io.BytesIO(file_bytes), filename=filename)
            await dest.send(content=caption if caption else None, file=file_obj)
            return True
        except Exception as exc:
            logger.error("[Discord] Gagal mengirim file ke %s (%s): %s", target_id, filename, exc)
            return False

    # =========================================================================
    # Lifecycle Pengelolaan Koneksi Bot Discord
    # =========================================================================

    async def start_bot(self) -> None:
        """Memulai Discord bot di background task asyncio."""
        if not self.token or "YOUR_DISCORD_BOT_TOKEN" in self.token:
            logger.warning("[Discord] Token Discord bot belum diset. Fitur Discord dinonaktifkan.")
            return

        self._is_running = True
        logger.info("[Discord] Menghubungkan ke Discord Gateway...")
        try:
            await self.bot.start(self.token)
        except asyncio.CancelledError:
            logger.info("[Discord] Task Discord bot dibatalkan secara normal.")
        except Exception as exc:
            logger.error("[Discord] Koneksi Discord bot terputus: %s", exc)

    async def stop_bot(self) -> None:
        """Menghentikan Discord bot secara graceful."""
        self._is_running = False
        if not self.bot.is_closed():
            logger.info("[Discord] Menutup koneksi Discord bot...")
            try:
                await self.bot.close()
            except Exception as exc:
                logger.warning("[Discord] Error saat menutup Discord bot: %s", exc)
