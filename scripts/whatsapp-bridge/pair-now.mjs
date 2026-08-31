// One-off WhatsApp pairing by PHONE CODE (no QR). Usage:
//   node pair-now.mjs <phoneIntlDigits>
// Prints JSON events to stdout: {ev:'pairing_code', code} then {ev:'paired_open'}.
// Uses same session dir as the real bridge so creds persist there.
import { makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } from '@whiskeysockets/baileys';
import pino from 'pino';

const SESSION_DIR = '/home/sebaescu/.hermes/whatsapp/session';
const PHONE = (process.argv[2] || '').replace(/\D/g, '');
if (!PHONE || PHONE.length < 10) {
  console.log(JSON.stringify({ ev: 'error', error: 'bad phone' }));
  process.exit(2);
}

const logger = pino({ level: 'silent' });
const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
const { version } = await fetchLatestBaileysVersion().catch(() => ({}));

const sock = makeWASocket({
  ...(version ? { version } : {}),
  auth: state,
  logger,
  printQRInTerminal: false,
  browser: ['Ubuntu', 'Chrome', '120.0'], // pair-code REQUIRES Ubuntu identity
  syncFullHistory: false,
  markOnlineOnConnect: false,
  getMessage: async () => ({ conversation: '' }),
});

let pairingRequested = false;
let timer = null;

async function requestPairing() {
  if (pairingRequested) return;
  pairingRequested = true;
  try {
    const code = await sock.requestPairingCode(PHONE);
    console.log(JSON.stringify({ ev: 'pairing_code', code }));
  } catch (e) {
    console.log(JSON.stringify({ ev: 'pairing_error', error: String(e?.message || e) }));
    process.exit(2);
  }
}

sock.ev.on('creds.update', saveCreds);
sock.ev.on('connection.update', async (u) => {
  const { connection, lastDisconnect, qr } = u;
  if (qr) requestPairing();
  if (connection === 'open') {
    if (timer) clearTimeout(timer);
    console.log(JSON.stringify({ ev: 'paired_open' }));
    // give creds time to persist
    setTimeout(() => process.exit(0), 4000);
  }
  if (connection === 'close') {
    const err = lastDisconnect?.error;
    const status = err?.output?.statusCode ?? err?.status ?? undefined;
    console.log(JSON.stringify({
      ev: 'close_detail',
      status,
      message: String(err?.message || err || 'unknown').slice(0, 200),
      data: err?.data ? String(JSON.stringify(err.data)).slice(0, 200) : undefined,
      isBoom: !!err?.isBoom,
    }));
    if (status === DisconnectReason.loggedOut) {
      console.log(JSON.stringify({ ev: 'logged_out' }));
      process.exit(1);
    }
    // 515 = restart requested right after pairing; just wait for 'open'
    if (status !== 515 && status !== 408) {
      process.exit(3);
    }
  }
});

// Fallback: if no qr event in 20s, request anyway
timer = setTimeout(() => requestPairing(), 20000);
