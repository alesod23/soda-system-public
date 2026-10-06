'use strict';
/*
 * notif-collapse.js: the two ways a notification stops bothering the owner once it is moot.
 *
 *  filterBatch(source, batch)  BEFORE classify: drops a message older than the stale window, one already answered
 *                              (the owner wrote later in that thread), and a text already shown on a card.
 *  tick(deps)                  AFTER push: a card whose every sender the owner answered after their message is edited in
 *                              place into one line "answered by you at HH:MM, closed" (notif ledger H3).
 *
 * The stale window is HIS number, read from the compiled notif skill on every call (H4 carries `stale_hours <n>` in its
 * rule text, which survives the compile), else NOTIF_STALE_HOURS, else 48.
 * Never throws into the daemon and never sends anything new: the only Telegram call is an edit of a card it pushed.
 */
const fs = require('fs');
const path = require('path');
const cp = require('child_process');
const S = require('../lib/soda_env');
const src = require('./sources/jsonl');

const SKILL = () => S.env('NOTIF_SKILL', path.join(S.SKILLS, 'notif', 'SKILL.md'));
const LOOKBACK_H = () => Number(S.env('NOTIF_COLLAPSE_LOOKBACK_H', '48'));
const MAX_TRIES = 3;

function staleHours() {
  try {
    const m = /stale_hours[:\s]+(\d+(?:\.\d+)?)/i.exec(fs.readFileSync(SKILL(), 'utf8'));
    if (m) return Number(m[1]);
  } catch {}
  return Number(S.env('NOTIF_STALE_HOURS', '48'));
}

function filterBatch(source, batch, { notifLog, lines, now = Date.now() } = {}) {
  const kept = [], dropped = [];
  const hours = staleHours();
  const store = lines || src.storeLines(source);
  let shown = new Set();
  try { if (notifLog) shown = notifLog.shownTexts(LOOKBACK_H(), now); } catch {}
  for (const m of batch || []) {
    const t = Date.parse(m.ts || '');
    if (Number.isFinite(t) && now - t > hours * 3600e3) { dropped.push({ m, why: `older than ${hours} h` }); continue; }
    if (src.answered(m, store)) { dropped.push({ m, why: 'answered later in the thread' }); continue; }
    if (m.text && shown.has(String(m.text).trim().toLowerCase())) { dropped.push({ m, why: 'already on a card' }); continue; }
    kept.push(m);
  }
  return { kept, dropped };
}

function hm(ms, tz) {
  try { return new Date(ms).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: tz || S.env('SODA_TZ', 'UTC') }); }
  catch { return new Date(ms).toISOString().slice(11, 16); }
}

// One pass over the cards pushed in the lookback window. deps: {notifLog, hit, push (path of push.js), esc, log, now,
// lines (channel -> store lines, for tests), edit (fn(message_id, html) for tests)}
function tick(deps) {
  const now = deps.now || Date.now();
  const out = { checked: 0, collapsed: 0, failed: 0 };
  let entries = [];
  try { entries = deps.notifLog.readAll(); } catch { return out; }
  const cut = now - LOOKBACK_H() * 3600e3;
  for (const e of entries) {
    if (e.collapsed || !e.tg_message_id || Date.parse(e.ts || '') < cut || (e.collapse_tries || 0) >= MAX_TRIES) continue;
    out.checked += 1;
    const store = (deps.lines && deps.lines[e.source]) || src.storeLines(e.source);
    const msgs = e.messages || [];
    if (!msgs.length) continue;
    let last = 0, all = true;
    for (const m of msgs) {
      const t = Date.parse(m.ts || '');
      const mine = store.filter((l) => l.from_me && l.thread === m.thread && Date.parse(l.ts || '') > t);
      if (!mine.length) { all = false; break; }
      last = Math.max(last, ...mine.map((l) => Date.parse(l.ts)));
    }
    if (!all) continue;
    const who = [...new Set(msgs.map((m) => m.sender))].join(', ');
    const line = `✓ notif · ${deps.esc ? deps.esc(e.source) : e.source} · ${deps.esc ? deps.esc(who) : who}: answered by you at ${hm(last)}, closed`;
    const ok = deps.edit ? deps.edit(e.tg_message_id, line) : editViaPush(deps.push, e.tg_message_id, line);
    if (ok) {
      out.collapsed += 1;
      deps.notifLog.setField(e.ts, { collapsed: new Date(now).toISOString(), collapsed_evidence: `from_me at ${new Date(last).toISOString()}` });
      deps.hit('H3', 'block', e.id, e.source, `collapsed: ${who} answered at ${new Date(last).toISOString()}`);
    } else {
      out.failed += 1;
      const tries = (e.collapse_tries || 0) + 1;
      deps.notifLog.setField(e.ts, { collapse_tries: tries });
      if (tries >= MAX_TRIES) deps.hit('H3', 'flag', e.id, e.source, `could not edit the card ${MAX_TRIES} times`);
    }
  }
  return out;
}

function editViaPush(pushJs, messageId, html) {
  const tmp = path.join(require('os').tmpdir(), `notif-collapse-${process.pid}-${Date.now()}.html`);
  try {
    fs.writeFileSync(tmp, html, 'utf8');
    const r = cp.spawnSync(process.execPath, [pushJs, '--file', tmp, '--html', '--edit', String(messageId), '--silent'], { encoding: 'utf8', timeout: 30000 });
    return r.status === 0;
  } catch { return false; } finally { try { fs.unlinkSync(tmp); } catch {} }
}

function start(depsFn) {
  const every = Number(S.env('NOTIF_COLLAPSE_EVERY_MS', '120000'));
  const t = setInterval(() => { try { tick(depsFn()); } catch (e) { try { depsFn().log('collapse tick failed: ' + e.message); } catch {} } }, every);
  return { every, stop: () => clearInterval(t) };
}

module.exports = { filterBatch, tick, start, staleHours };
