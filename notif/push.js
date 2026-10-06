'use strict';
/*
 * push.js: send (or edit) one notification card in the owner's Telegram chat, through the Bot API. Zero deps.
 *
 *   node push.js "text"                      plain text
 *   node push.js --file card.html --html     HTML parse mode (Telegram's subset: b, i, a, code, blockquote)
 *   node push.js --file card.html --html --edit 1234 [--silent]
 *   echo "text" | node push.js
 *
 * Prints `message_id=<n>` on success (the daemon keeps it so the card can be edited in place later).
 * Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (from the environment or <repo>/.env). Without a token it runs DRY: the
 * card is printed to stdout and `message_id=0` is returned, so the whole pipeline works offline.
 *
 * It only ever writes to the ONE chat in TELEGRAM_CHAT_ID (the owner's own chat). It never sends to anyone else.
 */
const fs = require('fs');
const https = require('https');
const { env } = require('../lib/soda_env');

function parseArgs(argv) {
  const a = { html: false, silent: false, edit: null, file: null, text: [] };
  for (let i = 0; i < argv.length; i++) {
    const x = argv[i];
    if (x === '--html') a.html = true;
    else if (x === '--silent') a.silent = true;
    else if (x === '--edit') a.edit = argv[++i];
    else if (x === '--file') a.file = argv[++i];
    else a.text.push(x);
  }
  return a;
}

function api(token, method, payload) {
  return new Promise((resolve) => {
    const body = JSON.stringify(payload);
    const req = https.request({
      hostname: 'api.telegram.org', path: `/bot${token}/${method}`, method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) }, timeout: 20000,
    }, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch { resolve({ ok: false, description: d.slice(0, 200) }); } });
    });
    req.on('error', (e) => resolve({ ok: false, description: e.message }));
    req.on('timeout', () => { req.destroy(); resolve({ ok: false, description: 'timeout' }); });
    req.end(body);
  });
}

async function push({ text, html = false, silent = false, edit = null }) {
  const token = env('TELEGRAM_BOT_TOKEN', '');
  const chat = env('TELEGRAM_CHAT_ID', '');
  if (!token || !chat) {
    process.stdout.write(`[dry: no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID]${edit ? ' (edit ' + edit + ')' : ''}\n${text}\n`);
    return { ok: true, message_id: edit ? Number(edit) : 0, dry: true };
  }
  const base = { chat_id: chat, text: String(text).slice(0, 4096), disable_web_page_preview: true };
  if (html) base.parse_mode = 'HTML';
  const r = edit
    ? await api(token, 'editMessageText', Object.assign(base, { message_id: Number(edit) }))
    : await api(token, 'sendMessage', Object.assign(base, { disable_notification: !!silent }));
  if (!r.ok) return { ok: false, error: r.description || 'telegram error' };
  return { ok: true, message_id: (r.result && r.result.message_id) || Number(edit) || 0 };
}

async function main() {
  const a = parseArgs(process.argv.slice(2));
  let text = a.file ? fs.readFileSync(a.file, 'utf8') : a.text.join(' ');
  if (!text && !process.stdin.isTTY) text = fs.readFileSync(0, 'utf8');
  if (!String(text).trim()) { process.stderr.write('push.js: empty message\n'); process.exit(2); }
  const r = await push({ text, html: a.html, silent: a.silent, edit: a.edit });
  if (!r.ok) { process.stderr.write('push.js: ' + r.error + '\n'); process.exit(1); }
  process.stdout.write(`message_id=${r.message_id}\n`);
}

module.exports = { push };
if (require.main === module) main();
