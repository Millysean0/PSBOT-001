/**
 * PSBOT-001: WhatsApp Bridge (Baileys)
 * Kompatibel dengan Windows, Linux, dan Android (Termux).
 * Berkomunikasi dengan core Python melalui JSON line di stdin/stdout.
 * 
 * Tanggung Jawab:
 * - Mengelola sesi WhatsApp (QR Code / Pairing Code)
 * - Menerima instruksi broadcast pengumuman (Absen, Tugas, Materi, SKEM)
 * - Menerima instruksi pengiriman file lampiran tugas & materi secara langsung
 * - Meneruskan perintah chat pengguna ke runtime Python
 */

const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
    makeCacheableSignalKeyStore,
} = require('@whiskeysockets/baileys');
const pino = require('pino');
const qrcode = require('qrcode-terminal');
const readline = require('readline');
const fs = require('fs');

// Redam log error internal libsignal (Bad MAC / session decryption) agar tidak memenuhi konsol
const origConsoleError = console.error;
console.error = function (...args) {
    const text = args.map(a => (typeof a === 'string' ? a : (a?.stack || a?.message || ''))).join(' ');
    if (
        text.includes('Bad MAC') ||
        text.includes('Failed to decrypt message') ||
        text.includes('Session error') ||
        text.includes('Closing open session in favor of incoming prekey bundle')
    ) {
        return;
    }
    origConsoleError.apply(console, args);
};

// Konfigurasi
const sessionDir = process.env.WA_SESSION_DIR || 'wa_session';
const phoneNumber = (process.env.WA_PHONE_NUMBER || '').trim();

let sock = null;
let isConnected = false;
const msgStore = new Map();

async function getStoredMessage(key) {
    if (key?.id && msgStore.has(key.id)) {
        return msgStore.get(key.id);
    }
    return undefined;
}

function sendEvent(obj) {
    try {
        process.stdout.write(JSON.stringify(obj) + '\n');
    } catch (e) {
        console.error('Error writing to stdout:', e.message);
    }
}

function getMessageText(msg) {
    const m = msg?.message;
    if (!m) return '';
    return (
        m.conversation ||
        m.extendedTextMessage?.text ||
        m.imageMessage?.caption ||
        m.videoMessage?.caption ||
        ''
    ).trim();
}

async function startWhatsApp() {
    try {
        const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
        const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: [2, 3000, 1015901307] }));

        sock = makeWASocket({
            version,
            logger: pino({ level: 'silent' }),
            printQRInTerminal: false,
            auth: {
                creds: state.creds,
                keys: makeCacheableSignalKeyStore(state.keys, pino({ level: 'silent' })),
            },
            browser: ['PSBOT-001', 'Chrome', '124.0.0.0'],
            generateHighQualityLinkPreview: false,
            syncFullHistory: false,
            getMessage: getStoredMessage,
            shouldIgnoreJid: jid => jid.endsWith('@broadcast'),
        });

        sock.ev.on('creds.update', saveCreds);

        // Pairing Code handling jika nomor telepon diatur dan belum login
        if (!sock.authState.creds.registered && phoneNumber) {
            setTimeout(async () => {
                try {
                    const cleanPhone = phoneNumber.replace(/\D/g, '');
                    if (cleanPhone) {
                        const code = await sock.requestPairingCode(cleanPhone);
                        sendEvent({ event: 'pairing_code', code: code });
                        console.error('\n' + '='.repeat(60));
                        console.error('📱 KODE TAUTKAN PERANGKAT WHATSAPP (PAIRING CODE):');
                        console.error(`👉  ${code}  👈`);
                        console.error('Langkah di HP:');
                        console.error('1. Buka WhatsApp > Perangkat Tertaut > Tautkan Perangkat.');
                        console.error('2. Pilih "Tautkan dengan nomor telepon saja".');
                        console.error(`3. Masukkan kode: ${code}`);
                        console.error('='.repeat(60) + '\n');
                    }
                } catch (err) {
                    console.error('Gagal meminta pairing code:', err.message);
                }
            }, 3000);
        }

        // Connection update handler
        sock.ev.on('connection.update', (update) => {
            const { connection, lastDisconnect, qr } = update;

            if (qr && !phoneNumber) {
                console.error('\nSilakan scan QR Code WhatsApp di bawah:');
                qrcode.generate(qr, { small: true }, (qrString) => {
                    console.error(qrString);
                });
                sendEvent({ event: 'qr', qr: qr });
            }

            if (connection === 'close') {
                isConnected = false;
                const statusCode = lastDisconnect?.error?.output?.statusCode;
                const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
                sendEvent({ event: 'disconnected', reason: statusCode });

                if (shouldReconnect) {
                    setTimeout(startWhatsApp, 3000);
                } else {
                    console.error('⚠️ WhatsApp Logout. Hapus folder wa_session untuk login ulang.');
                }
            } else if (connection === 'open') {
                isConnected = true;
                sendEvent({ event: 'connected', user: sock.user });
                console.error('✅ WhatsApp Baileys berhasil terhubung ke server WhatsApp!');
            }
        });

        // Message incoming handler
        sock.ev.on('messages.upsert', async ({ messages }) => {
            if (!messages || messages.length === 0) return;

            for (const msg of messages) {
                if (msg.key?.id && msg.message) {
                    msgStore.set(msg.key.id, msg.message);
                    if (msgStore.size > 250) {
                        const firstKey = msgStore.keys().next().value;
                        msgStore.delete(firstKey);
                    }
                }

                if (!msg.message || msg.key.fromMe) continue;

                const chatJid = msg.key.remoteJid;
                const isGroup = chatJid.endsWith('@g.us');
                const text = getMessageText(msg);

                if (text) {
                    sendEvent({
                        event: 'message',
                        chat_jid: chatJid,
                        is_group: isGroup,
                        text: text,
                        sender: msg.key.participant || chatJid,
                    });
                }
            }
        });

    } catch (err) {
        console.error('Error inisialisasi WhatsApp Baileys:', err);
        setTimeout(startWhatsApp, 5000);
    }
}

// Baca instruksi dari Python via stdin
const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: false,
});

rl.on('line', async (line) => {
    try {
        if (!line.trim()) return;
        const data = JSON.parse(line.trim());

        if (data.action === 'send') {
            const { chat_id, message, id } = data;
            if (!sock || !isConnected) {
                sendEvent({ action: 'sent', id: id, success: false, error: 'Not connected' });
                return;
            }

            let targetJid = chat_id;
            if (!targetJid.includes('@')) {
                targetJid = `${targetJid}@s.whatsapp.net`;
            }

            let mentions = [];
            if (targetJid.endsWith('@g.us') && message.includes('@all')) {
                try {
                    const groupMeta = await sock.groupMetadata(targetJid);
                    if (groupMeta && groupMeta.participants) {
                        mentions = groupMeta.participants.map(p => p.id);
                    }
                } catch (e) {
                    // abaikan jika gagal fetch metadata grup
                }
            }

            const sendPayload = mentions.length > 0 ? { text: message, mentions: mentions } : { text: message };
            await sock.sendMessage(targetJid, sendPayload);
            sendEvent({ action: 'sent', id: id, success: true });

        } else if (data.action === 'send_file') {
            const { chat_id, file_base64, filename, mimetype, caption, id } = data;
            if (!sock || !isConnected) {
                sendEvent({ action: 'sent', id: id, success: false, error: 'Not connected' });
                return;
            }

            let targetJid = chat_id;
            if (!targetJid.includes('@')) {
                targetJid = `${targetJid}@s.whatsapp.net`;
            }

            const fileBuffer = Buffer.from(file_base64, 'base64');
            const detectedMime = mimetype || 'application/octet-stream';

            await sock.sendMessage(targetJid, {
                document: fileBuffer,
                fileName: filename || 'file',
                mimetype: detectedMime,
                caption: caption || '',
            });
            sendEvent({ action: 'sent', id: id, success: true });

        } else if (data.action === 'status') {
            sendEvent({ action: 'status_response', is_connected: isConnected });
        }
    } catch (err) {
        sendEvent({ action: 'error', error: err.message });
    }
});

startWhatsApp();
