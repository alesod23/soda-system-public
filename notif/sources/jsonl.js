'use strict';
/*
 * sources/jsonl.js: the one generic notification source. It tails JSONL "stores", one per channel, and hands every
 * NEW incoming line to the daemon. A store is written by whatever poller you already have (an IMAP poll, a chat
 * export, a webhook receiver): the daemon never talks to a mail or chat service itself.
 *
 * Line format (one JSON object per line, append-only):
 *   {"id": "unique per store", "channel": "Email", "thread": "conversation id", "sender": "display name",
 *    "group": "group / list / mailbox name, or empty", "text": "the message (or its preview)",
 *    "ts": "ISO time it was written", "from_me": false}
 * Lines with from_me:true are the owner's own messages: never notified, but they are what proves a thread was
 * answered (the "answered" filter and the auto-collapse both read them).
 *
 * Config: NOTIF_STORES="Email=./data/notif/email-store.jsonl,Chat=./data/notif/chat-store.jsonl"
 * (channel name = the label shown on the card). Polls every NOTIF_POLL_MS (default 3000).
 * Starts at the END of each store: history is never re-notified after a restart.
 */
const fs = require('fs');
const path = require('path');
const { env, HOME } = require('../../lib/soda_env');

function parseStores(spec) {
  const def = `Email=${path.join(HOME, 'notif', 'email-store.jsonl')},Chat=${path.join(HOME, 'notif', 'chat-store.jsonl')}`;
  return String(spec || def).split(',').map((s) => s.trim()).filter(Boolean).map((s) => {
    const i = s.indexOf('=');
    return { channel: s.slice(0, i).trim(), file: path.resolve(s.slice(i + 1).trim()) };
  });
}

function readLines(file) {
  try {
    return fs.readFileSync(file, 'utf8').split('\n').filter((l) => l.trim()).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
  } catch { return []; }
}

// All lines of a store (for the answered check and the collapse tick).
function storeLines(channel, stores) {
  const s = (stores || parseStores(env('NOTIF_STORES'))).find((x) => x.channel === channel);
  return s ? readLines(s.file) : [];
}

// True when the owner wrote in the same thread AFTER this message.
function answered(msg, lines) {
  const t = Date.parse(msg.ts || '');
  return (lines || []).some((l) => l.from_me && l.thread && l.thread === msg.thread && Date.parse(l.ts || '') > t);
}

function startJsonlSources(onMessage, opts = {}) {
  const stores = parseStores(opts.spec || env('NOTIF_STORES'));
  const every = Number(env('NOTIF_POLL_MS', '3000'));
  const offsets = new Map();
  for (const s of stores) {
    try { offsets.set(s.file, fs.statSync(s.file).size); } catch { offsets.set(s.file, 0); }
  }
  const tick = () => {
    for (const s of stores) {
      let size = 0;
      try { size = fs.statSync(s.file).size; } catch { continue; }
      let off = offsets.get(s.file) || 0;
      if (size < off) off = 0;               // truncated / rotated: start over
      if (size === off) continue;
      const fd = fs.openSync(s.file, 'r');
      const buf = Buffer.alloc(size - off);
      fs.readSync(fd, buf, 0, buf.length, off);
      fs.closeSync(fd);
      const text = buf.toString('utf8');
      const cut = text.lastIndexOf('\n');
      if (cut < 0) continue;                  // a half-written line: wait for the rest
      offsets.set(s.file, off + Buffer.byteLength(text.slice(0, cut + 1)));
      for (const l of text.slice(0, cut).split('\n')) {
        if (!l.trim()) continue;
        let m; try { m = JSON.parse(l); } catch { continue; }
        if (m.from_me) continue;
        m.channel = m.channel || s.channel;
        try { onMessage(s.channel, m); } catch {}
      }
    }
  };
  const timer = setInterval(tick, every);
  if (timer.unref && opts.unref) timer.unref();
  return { stores, tick, stop: () => clearInterval(timer) };
}

module.exports = { startJsonlSources, parseStores, storeLines, answered, readLines };
