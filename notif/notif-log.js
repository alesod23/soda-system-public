'use strict';
// notif-log.js: the durable memory of every card the daemon pushed (append-only JSONL). The collapse tick and any
// session that has to resolve "what was that card about?" read it. setField() rewrites one entry in place (the
// Telegram message id arrives after the push; the collapse marks a card closed).
const fs = require('fs');
const path = require('path');
const S = require('../lib/soda_env');

const FILE = () => S.env('NOTIF_LOG', path.join(S.HOME, 'notif', 'notification-log.jsonl'));

function append(entry) { S.appendJsonl(FILE(), entry); }

function readAll() {
  try {
    return fs.readFileSync(FILE(), 'utf8').split('\n').filter((l) => l.trim()).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
  } catch { return []; }
}

function setField(ts, fields) {
  const all = readAll();
  let hit = false;
  for (const e of all) if (e.ts === ts) { Object.assign(e, fields); hit = true; }
  if (!hit) return false;
  const tmp = FILE() + '.tmp';
  fs.writeFileSync(tmp, all.map((e) => JSON.stringify(e)).join('\n') + '\n', 'utf8');
  fs.renameSync(tmp, FILE());
  return true;
}

// Every text already shown on a card in the last `hours` (a duplicate is never shown twice).
function shownTexts(hours = 48, now = Date.now()) {
  const cut = now - hours * 3600e3;
  const out = new Set();
  for (const e of readAll()) {
    if (Date.parse(e.ts || '') < cut) continue;
    for (const m of e.messages || []) if (m.text) out.add(String(m.text).trim().toLowerCase());
  }
  return out;
}

module.exports = { append, readAll, setField, shownTexts, FILE };
